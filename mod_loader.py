"""
mod_loader.py
Équivalent de modLoader.pl.
Supporte : Skyrim SE/LE/VR, Enderal/SE, Fallout 4 / 4 VR, Fallout New Vegas.
  - Lit ModOrganizer.ini pour trouver le profil sélectionné, le gamePath, etc.
  - Lit modlist.txt / plugins.txt / loadorder.txt
  - Parcourt les dossiers de mods (et décompresse les BSA/.ba2 via bsarch) pour détecter
    les dépendances logicielles (Script, SPID, KID, DLL, MCM, F4SE, NVSE, etc.)
  - Remplit mod_data et plugin_data
"""

import os
import re
import json
import logging
import hashlib
import subprocess
import tempfile
import shutil
from typing import Optional, Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes globales
# ---------------------------------------------------------------------------

# Toutes les catégories de soft-deps
SOFT_DEP_KEYS = [
    'Unknown', 'BaseGame', 'JSON', 'INI', 'Script', 'DLL', 'MCM Quest', 'MCM Helper',
    'Map', 'Preset', 'DAR', 'OAR', 'SPID', 'KID', 'BOS', 'SRD', 'CRD', 'CID', 'FML',
    'AOS', 'IPM', 'II', 'NAVI', 'Localized', 'SKSE', 'F4SE', 'NVSE', 'Large',
]

LARGE_PLUGIN_SIZE = 1_000_000  # 1 MB

# Extensions d'archive supportées (BSA pour TES/FNV, BA2 pour FO4)
ARCHIVE_EXTENSIONS = {'.bsa', '.ba2'}

# Extensions à ignorer complètement lors du scan
SKIP_EXTENSIONS = {
    '.mohidden', '.modgroups', '.dds', '.nif', '.hkx', '.exe', '.fuz',
}

# Patterns de fichiers à ignorer (case-insensitive)
SKIP_PATTERNS = [
    re.compile(r'meta\.ini$',                re.I),
    re.compile(r'readme.*\.txt$',            re.I),
    re.compile(r'\.html$',                   re.I),
    re.compile(r'EngineFixesVR_SNCT\.toml$', re.I),
]

# ---------------------------------------------------------------------------
# Tables de correspondance jeux
# ---------------------------------------------------------------------------

# Jeux qui supportent le flag ESL dans le header TES4
ESL_GAMES = {
    'Skyrim Special Edition', 'Skyrim Anniversary Edition',
    'Enderal Special Edition', 'Skyrim VR',
    'Fallout 4', 'Fallout 4 VR',
}

# gameName (dans ModOrganizer.ini) → game_id interne
GAME_NAME_TO_ID: dict[str, str] = {
    'Skyrim Special Edition':  'skyrimse',
    'Skyrim Anniversary Edition': 'skyrimse',
    'Skyrim':                  'skyrim',
    'Skyrim VR':               'skyrimvr',
    'Enderal':                 'enderal',
    'Enderal Special Edition': 'enderalse',
    'Fallout 4':               'fallout4',
    'Fallout 4 VR':            'fallout4vr',
    # MO2 affiche "New Vegas" pour Fallout New Vegas
    'New Vegas':               'falloutnv',
    'Fallout New Vegas':       'falloutnv',
    'Fallout NV':              'falloutnv',
}

# game_id → nom du script-extender associé
GAME_SCRIPT_EXTENDER: dict[str, str] = {
    'skyrimse':   'SKSE',
    'skyrim':     'SKSE',
    'skyrimvr':   'SKSE',
    'enderal':    'SKSE',
    'enderalse':  'SKSE',
    'fallout4':   'F4SE',
    'fallout4vr': 'F4SE',
    'falloutnv':  'NVSE',
}

# ---------------------------------------------------------------------------
# Support Vortex : gameId → (appdata_subfolder, game_name, respect_esl)
# ---------------------------------------------------------------------------
VORTEX_GAME_MAP: dict[str, tuple[str, str, bool]] = {
    'skyrimse':   ('Skyrim Special Edition',  'Skyrim Special Edition',  True),
    'skyrim':     ('Skyrim',                  'Skyrim',                  False),
    'skyrimvr':   ('Skyrim VR',               'Skyrim VR',               True),
    'enderal':    ('Enderal',                 'Enderal',                 False),
    'enderalse':  ('Enderal Special Edition', 'Enderal Special Edition', True),
    'fallout4':   ('Fallout4',                'Fallout 4',               True),
    'fallout4vr': ('Fallout4VR',              'Fallout 4 VR',            True),
    'falloutnv':  ('FalloutNV',               'Fallout New Vegas',       False),
}


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

def _fix_plugin_cap(name: str, loadorder: list[str]) -> str:
    nl = name.lower()
    for p in loadorder:
        if p.lower() == nl:
            return p
    return name


def _should_skip_path(path: str) -> bool:
    """Retourne True si le fichier/dossier doit être ignoré."""
    basename = os.path.basename(path)
    ext = os.path.splitext(basename)[1].lower()
    if ext in SKIP_EXTENSIONS:
        return True
    for pat in SKIP_PATTERNS:
        if pat.search(basename):
            return True
    norm = path.replace('\\', '/')
    if '/optional/' in norm.lower():
        return True
    if re.search(r'/mods/Smash ', path):
        return True
    if re.search(r'/mods/Merge \d', path):
        return True
    return False


def _classify_file(path: str) -> str:
    """
    Retourne la catégorie soft-dep d'un fichier.
    Note : les DLL sont classifiées ici comme 'DLL' (générique) ;
    la clé précise (SKSE/F4SE/NVSE) est déterminée dans _scan_file
    selon le chemin d'installation du script-extender.
    """
    norm = path.replace('\\', '/')
    b    = os.path.basename(path)
    ext  = os.path.splitext(b)[1].lower()

    if ext == '.json':
        if re.search(r'/MCM/Config/[^/]*/config\.json$',   norm, re.I):
            return 'MCM Helper'
        if re.search(r'/MapMarkers/[^/]*\.json$',           norm, re.I):
            return 'Map'
        return 'JSON'
    if b.lower().endswith('_distr.ini'):  return 'SPID'
    if b.lower().endswith('_kid.ini'):    return 'KID'
    if b.lower().endswith('_swap.ini'):   return 'BOS'
    if b.lower().endswith('_srd.ini'):    return 'SRD'
    if b.lower().endswith('_crd.ini'):    return 'CRD'
    if b.lower().endswith('_cid.ini'):    return 'CID'
    if b.lower().endswith('_fml.ini'):    return 'FML'
    if b.lower().endswith('_anio.ini'):   return 'AOS'
    if b.lower().endswith('_ipm.ini'):    return 'IPM'
    if ext == '.ini':                     return 'INI'
    if ext in ('.psc', '.pex'):           return 'Script'
    if ext == '.dll':                     return 'DLL'
    if ext == '.jslot':                   return 'Preset'
    if re.search(r'/_conditions\.txt$',  norm, re.I):      return 'DAR'
    if re.search(r'OpenAnimationReplacer', norm, re.I):    return 'OAR'
    if re.search(r'InventoryInjector',   norm, re.I):      return 'II'
    return 'Unknown'


_PLUGIN_REF_TEXT = re.compile(r'([a-zA-Z0-9_\-. ()\[\]]+\.es[mpl])', re.I)
_PLUGIN_REF_PEX  = re.compile(rb'([a-zA-Z0-9_\-. ()\[\]]+\.es[mpl])', re.I)


def _extract_plugin_refs_text(content: str, loadorder: list[str]) -> list[str]:
    results = []
    for m in _PLUGIN_REF_TEXT.finditer(content):
        p = m.group(1).strip()
        if len(p) > 4:
            results.append(_fix_plugin_cap(p, loadorder))
    return results


def _extract_plugin_refs_pex(content: bytes, loadorder: list[str]) -> list[str]:
    results = []
    for m in _PLUGIN_REF_PEX.finditer(content):
        try:
            p = m.group(1).decode('latin-1').strip()
            if len(p) > 4:
                results.append(_fix_plugin_cap(p, loadorder))
        except Exception:
            continue
    return results


def _detect_se_key(path: str) -> str:
    """
    Détermine quelle clé de script-extender utiliser pour une DLL
    en analysant son chemin d'installation.
      F4SE/Plugins/  → 'F4SE'
      NVSE/Plugins/  → 'NVSE'
      SKSE/Plugins/  → 'SKSE'  (défaut)
    """
    norm_up = path.upper().replace('\\', '/')
    if '/F4SE/PLUGINS/' in norm_up or '/F4SE/PLUGIN/' in norm_up:
        return 'F4SE'
    if '/NVSE/PLUGINS/' in norm_up or '/NVSE/PLUGIN/' in norm_up:
        return 'NVSE'
    # SKSE ou toute autre DLL
    return 'SKSE'


def _scan_file(path: str, mod_name: str, mod_data: dict,
               loadorder: list[str], current_mod_loading: str) -> None:
    """Scanne un fichier pour détecter les dépendances logicielles."""
    entry   = mod_data[current_mod_loading]
    basename = os.path.basename(path)
    ext      = os.path.splitext(basename)[1].lower()

    # DLL → clé SKSE / F4SE / NVSE selon le dossier d'installation
    if ext == '.dll':
        se_key = _detect_se_key(path)
        entry.setdefault(se_key, [])
        entry[se_key].append(path)
        if re.search(r'MergeMapper\.dll$', path, re.I):
            entry['_has_mergemapper'] = True

    # Plugin dans le dossier mods
    m = re.search(r'[/\\]mods[/\\]([^/\\]*)[/\\]([^/\\]*\.es[mpl])$', path, re.I)
    if m:
        plugin_raw  = m.group(2)
        plugin_name = _fix_plugin_cap(plugin_raw, loadorder)
        entry.setdefault('Plugins', {})[plugin_name] = path
        if os.path.getsize(path) >= LARGE_PLUGIN_SIZE:
            entry.setdefault('Large', {})[plugin_name] = True

    is_text          = ext in ('.psc', '.json', '.ini', '.jslot', '.txt', '.toml')
    is_binary_script = (ext == '.pex')

    if not (is_text or is_binary_script):
        return

    try:
        if is_binary_script:
            with open(path, 'rb') as f:
                content_bytes = f.read()
            if b'SKI_ConfigBase' in content_bytes:
                entry['MCM Quest'] = 1
            refs = _extract_plugin_refs_pex(content_bytes, loadorder)
        else:
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    content_str = f.read()
            except Exception:
                return
            if 'SKI_ConfigBase' in content_str:
                entry['MCM Quest'] = 1
            refs = _extract_plugin_refs_text(content_str, loadorder)

        category = _classify_file(path)
        # Les DLL sont déjà traitées ci-dessus avec leur vraie clé SE
        if category not in ('DLL', 'NAVI', 'Localized', 'SKSE', 'MCM Quest'):
            for p in refs:
                entry.setdefault(category, {})[p] = True

    except Exception as e:
        log.debug(f'Error scanning {path}: {e}')


def _extract_bsa(bsa_path: str, bsarch_exe: str, loadorder: list[str],
                 mod_data: dict, current_mod_loading: str) -> None:
    """Décompresse un BSA ou BA2 dans un répertoire temporaire et le scanne."""
    tmpdir = tempfile.mkdtemp(prefix='koro_bsa_')
    try:
        cmd = [bsarch_exe, 'unpack', bsa_path, tmpdir, '-quiet']
        subprocess.run(cmd, timeout=120, capture_output=True)
        for root, dirs, files in os.walk(tmpdir):
            for fname in files:
                fpath = os.path.join(root, fname)
                if _should_skip_path(fpath):
                    continue
                _scan_file(fpath, current_mod_loading, mod_data,
                           loadorder, current_mod_loading)
    except Exception as e:
        log.warning(f'Archive extraction failed for {bsa_path}: {e}')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _scan_mod_folder(mod_path: str, bsarch_exe: str, loadorder: list[str],
                     mod_data: dict, current_mod_loading: str) -> None:
    """Scanne récursivement un dossier de mod. Gère BSA et BA2."""
    for root, dirs, files in os.walk(mod_path):
        dirs[:] = [d for d in dirs
                   if not re.search(r'[/\\]optional[/\\]?$',
                                    os.path.join(root, d), re.I)]
        for fname in files:
            fpath = os.path.join(root, fname)
            if _should_skip_path(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext in ARCHIVE_EXTENSIONS and bsarch_exe and os.path.isfile(bsarch_exe):
                _extract_bsa(fpath, bsarch_exe, loadorder, mod_data, current_mod_loading)
            else:
                _scan_file(fpath, current_mod_loading, mod_data,
                           loadorder, current_mod_loading)


# ---------------------------------------------------------------------------
# Initialisation d'une entrée mod_data vierge
# ---------------------------------------------------------------------------

def _new_mod_entry() -> dict:
    """Retourne un dict mod_data initialisé proprement pour tous les jeux."""
    return {
        'Installed': '',
        'Plugins':   {},
        **{k: {} for k in SOFT_DEP_KEYS
           if k not in ('NAVI', 'Localized', 'SKSE', 'F4SE', 'NVSE', 'MCM Quest')},
        'SKSE':      [],
        'F4SE':      [],
        'NVSE':      [],
        'MCM Quest': 0,
    }


# ---------------------------------------------------------------------------
# Lecture de ModOrganizer.ini
# ---------------------------------------------------------------------------

def parse_mo_ini(mo_ini_path: str) -> dict:
    """
    Lit ModOrganizer.ini et retourne la configuration essentielle,
    y compris le game_id et le nom du script-extender associé.
    """
    result: dict = {
        'modir':            os.path.dirname(mo_ini_path) + os.sep,
        'base_directory':   '',
        'selected_profile': '',
        'game_path':        '',
        'game_name':        '',
        'game_id':          '',
        'se_name':          'SKSE',
        'respect_esl':      False,
    }
    try:
        with open(mo_ini_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                m = re.match(r'^selected_profile=@ByteArray\((.*)\)', line)
                if m:
                    result['selected_profile'] = m.group(1).strip()
                m = re.match(r'^gamePath=@ByteArray\((.*)\)', line)
                if m:
                    result['game_path'] = m.group(1).strip().replace('\\\\', '\\')
                m = re.match(r'^gameName=(.*)', line)
                if m:
                    result['game_name'] = m.group(1).strip()

                m = re.match(r'^base_directory=(.*)', line)
                if m:
                    result['base_directory'] = os.path.normpath(m.group(1).strip())
    except Exception as e:
        log.error(f'Cannot read ModOrganizer.ini: {e}')

    if result.get('base_directory'):
        result['modir'] = result['base_directory'] + os.sep

    game_name = result['game_name']

    # Résoudre game_id depuis le nom exact, sinon via correspondance partielle
    game_id = GAME_NAME_TO_ID.get(game_name, '')
    if not game_id:
        gn_l = game_name.lower()
        if 'fallout 4' in gn_l or 'fallout4' in gn_l:
            game_id = 'fallout4'
        elif 'new vegas' in gn_l or 'falloutnv' in gn_l or 'fallout nv' in gn_l:
            game_id = 'falloutnv'
        elif 'enderal special' in gn_l:
            game_id = 'enderalse'
        elif 'enderal' in gn_l:
            game_id = 'enderal'
        elif 'skyrim vr' in gn_l:
            game_id = 'skyrimvr'
        elif 'skyrim' in gn_l:
            game_id = 'skyrimse'
        else:
            game_id = 'skyrimse'   # fallback ultime

    result['game_id']    = game_id
    result['respect_esl'] = game_name in ESL_GAMES
    result['se_name']    = GAME_SCRIPT_EXTENDER.get(game_id, 'SKSE')

    log.info(
        f"MO2 — Jeu : {game_name or 'inconnu'} "
        f"(game_id={game_id}, SE={result['se_name']}, "
        f"ESL={'oui' if result['respect_esl'] else 'non'})"
    )
    return result


# ---------------------------------------------------------------------------
# Support Vortex
# ---------------------------------------------------------------------------

def detect_vortex_games(vortex_appdata: str = '') -> list[dict]:
    """
    Détecte les jeux Bethesda configurés dans Vortex.
    Retourne une liste de dicts avec id, name, profiles_dir, staging_dir.
    """
    if not vortex_appdata:
        vortex_appdata = os.path.join(os.environ.get('APPDATA', ''), 'Vortex')

    games = []
    for game_id, (appdata_name, game_name, esl) in VORTEX_GAME_MAP.items():
        game_dir     = os.path.join(vortex_appdata, game_id)
        profiles_dir = os.path.join(game_dir, 'profiles')
        staging_dir  = os.path.join(game_dir, 'mods')
        if os.path.isdir(profiles_dir):
            games.append({
                'id':           game_id,
                'name':         game_name,
                'profiles_dir': profiles_dir,
                'staging_dir':  staging_dir,
                'respect_esl':  esl,
                'se_name':      GAME_SCRIPT_EXTENDER.get(game_id, 'SKSE'),
            })
    return games


def detect_vortex_profiles(profiles_dir: str) -> list[str]:
    """Retourne la liste des profils Vortex (dossiers contenant plugins.txt)."""
    profiles = []
    if not os.path.isdir(profiles_dir):
        return profiles
    for entry in os.scandir(profiles_dir):
        if entry.is_dir() and os.path.isfile(
                os.path.join(entry.path, 'plugins.txt')):
            profiles.append(entry.name)
    return profiles


def _vortex_game_appdata(game_id: str) -> str:
    """
    Retourne le dossier %LOCALAPPDATA%/<sous-dossier> ou le jeu stocke
    plugins.txt et loadorder.txt (synchronise par Vortex).
    """
    appdata_name = VORTEX_GAME_MAP.get(game_id, ('', '', False))[0]
    local = os.environ.get('LOCALAPPDATA', '')
    return os.path.join(local, appdata_name) if appdata_name else ''


# ---------------------------------------------------------------------------
# Propagation des dépendances SE vers plugin_data
# ---------------------------------------------------------------------------

def _propagate_se_to_plugins(mod_entry: dict, plugin_data: dict,
                              plugin_name: str) -> None:
    """
    Reporte les flags de script-extender (SKSE/F4SE/NVSE)
    depuis mod_data vers plugin_data[plugin_name].
    """
    for se_key in ('SKSE', 'F4SE', 'NVSE'):
        if mod_entry.get(se_key):
            plugin_data[plugin_name][se_key] = True
    if mod_entry.get('MCM Quest') == 1:
        plugin_data[plugin_name]['MCM Quest'] = True


# ---------------------------------------------------------------------------
# Filtres de plugins à exclure du loadorder traité
# ---------------------------------------------------------------------------

_EXCLUDE_PATTERNS = [
    re.compile(r'^(Smash )?(Merge \d|Patch)',        re.I),
    re.compile(r'Synthesis\.es[mlp]$',               re.I),
    re.compile(r'FNIS\.es[mlp]$',                    re.I),
    re.compile(r'DynDOLOD\.es[mlp]$',               re.I),
    re.compile(r'Occlusion\.es[mlp]$',               re.I),
    re.compile(r'NPC Appearances Merged\.es[mlp]$',  re.I),
]


def _is_excluded_plugin(name: str) -> bool:
    return any(p.search(name) for p in _EXCLUDE_PATTERNS)


# ---------------------------------------------------------------------------
# Lecture des fichiers de profil
# ---------------------------------------------------------------------------

def read_modlist(modir: str, profile: str) -> list[str]:
    """Lit modlist.txt → liste ordonnée des mods activés."""
    path = os.path.join(modir, 'profiles', profile, 'modlist.txt')
    mods = []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.rstrip('\n\r')
                if not line.startswith('+'):
                    continue
                mod = line[1:]
                if re.search(r'^.(Smash )?(Merge \d|Patch)', line):
                    continue
                if line.endswith('_separator'):
                    continue
                mods.insert(0, mod)
    except Exception as e:
        log.error(f'Cannot read modlist.txt: {e}')
    return mods


def read_plugins_and_loadorder(modir: str, profile: str,
                                game_path: str,
                                plugin_data: dict) -> tuple[list[str], int]:
    """Lit plugins.txt et loadorder.txt."""
    enabled_plugins: dict[str, int] = {}
    plugins_path = os.path.join(modir, 'profiles', profile, 'plugins.txt')
    try:
        with open(plugins_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.rstrip('\n\r')
                m = re.match(r'^([*]?)(.*\.es[mpl])$', line, re.I)
                if not m:
                    continue
                enabled, name = m.group(1), m.group(2)
                enabled_plugins[name] = 2
                if _is_excluded_plugin(name):
                    continue
                if enabled == '*':
                    enabled_plugins[name] = 1
    except Exception as e:
        log.error(f'Cannot read plugins.txt: {e}')

    loadorder: list[str] = []
    game_modules = 0
    lo_path = os.path.join(modir, 'profiles', profile, 'loadorder.txt')
    try:
        with open(lo_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.rstrip('\n\r')
                m = re.match(r'^(.*\.es[mpl])$', line, re.I)
                if not m:
                    continue
                name = m.group(1)
                if name in enabled_plugins and enabled_plugins[name] == 2:
                    continue
                if name not in enabled_plugins:
                    game_modules += 1
                loadorder.append(name)
                plugin_data[name] = {
                    'Name':     name,
                    'Location': os.path.join(game_path, 'Data', name),
                    'BaseGame': name not in enabled_plugins,
                    'Masters':  [],
                    'Children': [],
                }
    except Exception as e:
        log.error(f'Cannot read loadorder.txt: {e}')

    return loadorder, game_modules


# ---------------------------------------------------------------------------
# Chargement principal MO2
# ---------------------------------------------------------------------------

def load_mods(mo_ini_path: str, bsarch_exe: str,
              mod_data: dict, plugin_data: dict,
              progress_cb: Optional[Callable] = None) -> tuple[list[str], dict]:
    """Point d'entrée principal MO2 : charge et scanne les mods."""
    mo       = parse_mo_ini(mo_ini_path)
    modir    = mo['modir']
    profile  = mo['selected_profile']
    gamepath = mo['game_path']

    modlist  = read_modlist(modir, profile)
    loadorder, game_modules = read_plugins_and_loadorder(
        modir, profile, gamepath, plugin_data)

    total = len(modlist)
    for i, mod_name in enumerate(modlist):
        if progress_cb:
            progress_cb(i + 1, total, mod_name, 'mod')

        if mod_name not in mod_data:
            mod_data[mod_name] = _new_mod_entry()

        install_time = ''
        meta_path = os.path.join(modir, 'mods', mod_name, 'meta.ini')
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        m = re.match(r'^lastNexusQuery=(.*)$', line, re.I)
                        if m:
                            install_time = m.group(1).strip()
                        m = re.match(r'^nexusLastModified=(.*)$', line, re.I)
                        if m and not install_time:
                            install_time = m.group(1).strip()
            except Exception:
                pass

        if not (install_time and install_time == mod_data[mod_name].get('Installed', '')):
            mod_folder = os.path.join(modir, 'mods', mod_name)
            if os.path.isdir(mod_folder):
                _scan_mod_folder(mod_folder, bsarch_exe, loadorder,
                                 mod_data, mod_name)
            mod_data[mod_name]['Installed'] = install_time

        for p_raw, p_loc in mod_data[mod_name].get('Plugins', {}).items():
            p = _fix_plugin_cap(p_raw, loadorder)
            if p not in plugin_data:
                plugin_data[p] = {'Name': p, 'Masters': [], 'Children': []}
            plugin_data[p]['Location'] = p_loc
            _propagate_se_to_plugins(mod_data[mod_name], plugin_data, p)

        for cat in SOFT_DEP_KEYS:
            if cat in ('NAVI', 'Localized', 'SKSE', 'F4SE', 'NVSE', 'MCM Quest'):
                continue
            for p_raw in mod_data[mod_name].get(cat, {}):
                p = _fix_plugin_cap(p_raw, loadorder)
                if p not in plugin_data:
                    plugin_data[p] = {'Name': p, 'Masters': [], 'Children': []}
                plugin_data[p][cat] = True

    plugin_locations = {
        p: plugin_data[p].get('Location', '')
        for p in loadorder
    }

    mo['loadorder']        = loadorder
    mo['game_modules']     = game_modules
    mo['plugin_locations'] = plugin_locations

    log.info(
        f"MO2 — {mo['game_name']}, {len(loadorder)} plugins, "
        f"{len(modlist)} mods"
    )
    return loadorder, mo


# ---------------------------------------------------------------------------
# Chargement principal Vortex
# ---------------------------------------------------------------------------

def load_vortex(game_id: str, profile_id: str, staging_dir: str,
                game_path: str, bsarch_exe: str,
                mod_data: dict, plugin_data: dict,
                progress_cb: Optional[Callable] = None) -> tuple[list[str], dict]:
    """
    Point d'entrée Vortex : équivalent de load_mods() pour MO2.
    Les plugins.txt et loadorder.txt sont lus depuis le profil Vortex
    ou, en fallback, depuis %LOCALAPPDATA%/<jeu>/.
    """
    vortex_appdata = os.path.join(os.environ.get('APPDATA', ''), 'Vortex')
    profiles_dir   = os.path.join(vortex_appdata, game_id, 'profiles')
    profile_dir    = os.path.join(profiles_dir, profile_id)

    game_info   = VORTEX_GAME_MAP.get(game_id, ('', game_id, False))
    game_name   = game_info[1]
    respect_esl = game_info[2]
    se_name     = GAME_SCRIPT_EXTENDER.get(game_id, 'SKSE')

    mo: dict = {
        'manager':     'vortex',
        'game_id':     game_id,
        'game_name':   game_name,
        'game_path':   game_path,
        'staging_dir': staging_dir,
        'profile_id':  profile_id,
        'respect_esl': respect_esl,
        'se_name':     se_name,
    }

    # --- Lire plugins.txt et loadorder.txt ---
    plugins_path = os.path.join(profile_dir, 'plugins.txt')
    lo_path      = os.path.join(profile_dir, 'loadorder.txt')

    # Fallback : dossier système du jeu
    if not os.path.isfile(plugins_path):
        sys_dir      = _vortex_game_appdata(game_id)
        plugins_path = os.path.join(sys_dir, 'plugins.txt')
        lo_path      = os.path.join(sys_dir, 'loadorder.txt')

    enabled_plugins: dict[str, int] = {}
    try:
        with open(plugins_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.rstrip('\n\r')
                m = re.match(r'^([*]?)(.*\.es[mpl])$', line, re.I)
                if not m:
                    continue
                star, name = m.group(1), m.group(2)
                if _is_excluded_plugin(name):
                    continue
                enabled_plugins[name] = 1 if star == '*' else 2
    except Exception as e:
        log.error(f'Vortex: Cannot read plugins.txt ({plugins_path}): {e}')

    loadorder: list[str] = []
    game_modules = 0
    try:
        with open(lo_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.rstrip('\n\r')
                m = re.match(r'^(.*\.es[mpl])$', line, re.I)
                if not m:
                    continue
                name = m.group(1)
                if name in enabled_plugins and enabled_plugins[name] == 2:
                    continue
                is_base = name not in enabled_plugins
                if is_base:
                    game_modules += 1
                loadorder.append(name)
                plugin_data[name] = {
                    'Name':     name,
                    'Location': os.path.join(game_path, 'Data', name),
                    'BaseGame': is_base,
                    'Masters':  [],
                    'Children': [],
                }
    except Exception as e:
        log.error(f'Vortex: Cannot read loadorder.txt ({lo_path}): {e}')

    # --- Scanner le staging folder ---
    mod_folders: list[str] = []
    if os.path.isdir(staging_dir):
        try:
            mod_folders = [
                e.name for e in os.scandir(staging_dir)
                if e.is_dir() and not e.name.startswith('.')
            ]
        except Exception as e:
            log.warning(f'Vortex: Cannot scan staging dir: {e}')

    mod_folders = [m for m in mod_folders if not _is_excluded_plugin(m)]

    total = len(mod_folders)
    for i, mod_name in enumerate(mod_folders):
        if progress_cb:
            progress_cb(i + 1, total, mod_name, 'mod')

        if mod_name not in mod_data:
            mod_data[mod_name] = _new_mod_entry()

        mod_folder = os.path.join(staging_dir, mod_name)
        mtime = str(os.path.getmtime(mod_folder)) if os.path.isdir(mod_folder) else ''
        if mtime != mod_data[mod_name].get('Installed', ''):
            _scan_mod_folder(mod_folder, bsarch_exe, loadorder,
                             mod_data, mod_name)
            mod_data[mod_name]['Installed'] = mtime

        for p_raw, p_loc in mod_data[mod_name].get('Plugins', {}).items():
            p = _fix_plugin_cap(p_raw, loadorder)
            if p not in plugin_data:
                plugin_data[p] = {'Name': p, 'Masters': [], 'Children': []}
            plugin_data[p]['Location'] = p_loc
            _propagate_se_to_plugins(mod_data[mod_name], plugin_data, p)

        for cat in SOFT_DEP_KEYS:
            if cat in ('NAVI', 'Localized', 'SKSE', 'F4SE', 'NVSE', 'MCM Quest'):
                continue
            for p_raw in mod_data[mod_name].get(cat, {}):
                p = _fix_plugin_cap(p_raw, loadorder)
                if p not in plugin_data:
                    plugin_data[p] = {'Name': p, 'Masters': [], 'Children': []}
                plugin_data[p][cat] = True

    plugin_locations = {
        p: plugin_data[p].get('Location', '')
        for p in loadorder
    }

    mo['loadorder']        = loadorder
    mo['game_modules']     = game_modules
    mo['plugin_locations'] = plugin_locations

    log.info(
        f"Vortex — {game_name}, {len(loadorder)} plugins, "
        f"{len(mod_folders)} mods dans le staging."
    )
    return loadorder, mo


# MaxMerge V3.1 additions
def detect_archive_flag(mod_data):
    for mod,mdata in mod_data.items():
        for f in mdata.get('Files',[]):
            lf=f.lower()
            if lf.endswith('.bsa') or lf.endswith('.ba2'):
                mdata['Archive']=True
