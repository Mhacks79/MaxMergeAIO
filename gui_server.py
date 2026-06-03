"""
gui_server.py
Serveur Flask + Flask-SocketIO.
Remplace le bus IPC Perl entier + la GUI Tk de GUI.pl.
Supporte MO2 et Vortex pour Skyrim SE/LE/VR, Enderal, Fallout 4, Fallout NV.
"""

import json
import logging
import os
import threading
import webbrowser
import time
import sys
from typing import Optional

from flask import Flask, render_template_string, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit

import cache
from mod_loader    import (load_mods, load_vortex,
                           detect_vortex_games, detect_vortex_profiles,
                           parse_mo_ini, VORTEX_GAME_MAP, GAME_SCRIPT_EXTENDER)
from plugin_loader import load_all_plugins
from conflict_finder import ConflictFinder
from mergeable       import MergeableAnalyzer
from auto_sort       import AutoSorter

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Initialisation Flask (une seule fois, avec détection PyInstaller)
# ---------------------------------------------------------------------------

if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    _STATIC = os.path.join(sys._MEIPASS, 'static')
else:
    _HERE   = os.path.dirname(os.path.abspath(__file__))
    _STATIC = os.path.join(_HERE, 'static')

app = Flask(__name__, static_folder=_STATIC)
app.config['SECRET_KEY'] = 'koro-secret'

socketio = SocketIO(
    app,
    cors_allowed_origins='*',
    async_mode='threading',
    ping_timeout=60,
    ping_interval=25,
)


# ---------------------------------------------------------------------------
# État global de l'application
# ---------------------------------------------------------------------------

class AppState:
    def __init__(self):
        self.mo_ini_path:     str  = ''
        self.bsarch_path:     str  = ''
        # Manager
        self.manager:         str  = 'mo2'   # 'mo2' | 'vortex'
        self.vortex_game_id:  str  = ''
        self.vortex_profile:  str  = ''
        self.vortex_staging:  str  = ''
        self.vortex_gamepath: str  = ''
        # Informations jeu (renseignées après chargement)
        self.game_name:       str  = ''
        self.game_id:         str  = ''
        self.se_name:         str  = 'SKSE'  # script-extender du jeu
        self.respect_esl:     bool = False

        self.loadorder:    list = []
        self.game_modules: int  = 0
        self.mod_data:     dict = {}
        self.plugin_data:  dict = {}
        self.form_ids:     dict = {}
        self.mo_config:    dict = {}
        self.selection:    int  = 0
        self.merge_groups: list = []
        self.gui_list:     list = []
        self.colors:       list = []
        self.mg_conflicts: list = []
        self.has_merge_mapper: bool = False
        self.phase:        str  = 'idle'
        self.status_msg:   str  = ''
        self.active_count: tuple = (0, 0)

        self.cf:     Optional[ConflictFinder]    = None
        self.ma:     Optional[MergeableAnalyzer] = None
        self.sorter: Optional[AutoSorter]        = None


state     = AppState()
_work_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers d'émission WebSocket
# ---------------------------------------------------------------------------

def emit_status(msg: str, phase: str = None):
    state.status_msg = msg
    if phase:
        state.phase = phase
    socketio.emit('status', {'msg': msg, 'phase': state.phase})


def emit_list():
    active = sum(1 for row in state.gui_list if row[0] == 0)
    total  = len(state.gui_list)
    state.active_count = (active, total)
    socketio.emit('guilist', {
        'list':      state.gui_list,
        'selection': state.selection,
        'loadorder': state.loadorder,
        'active':    active,
        'total':     total,
    })


def emit_colors():
    socketio.emit('colors', {
        'colors':      state.colors,
        'mgConflicts': state.mg_conflicts,
    })


def _ready_payload() -> dict:
    """Construit le payload complet envoyé avec l'événement 'ready'."""
    active, total = state.active_count
    return {
        'active':    active,
        'total':     total,
        'game':      state.game_name,
        'game_id':   state.game_id,
        'se_name':   state.se_name,
    }


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

def _progress_cb(current, total, name, kind=''):
    pct = int(100 * current / total) if total else 0
    msg = f'{"Mod" if kind == "mod" else "Plugin"} ({current}/{total}): {name}'
    emit_status(msg)


def _prel_progress_cb(pct: int):
    state.status_msg = f'Calcul des relations : {pct}%'
    socketio.emit('status', {
        'msg':   state.status_msg,
        'phase': 'computing',
        'pct':   pct,
    })


def run_pipeline():
    with _work_lock:
        try:
            _run_pipeline_inner()
        except Exception as e:
            log.exception('Pipeline error')
            emit_status(f'Erreur : {e}', 'error')


def _run_pipeline_inner():
    # ── 0. Détection rapide du jeu (avant tout chargement de cache) ────────
    # Nécessaire pour choisir le bon sous-dossier de cache par jeu.
    emit_status('Détection du jeu…', 'loading')
    if state.manager == 'vortex':
        gid         = state.vortex_game_id
        game_info   = VORTEX_GAME_MAP.get(gid, ('', gid, False))
        state.game_name   = game_info[1]
        state.game_id     = gid
        state.respect_esl = game_info[2]
        state.se_name     = GAME_SCRIPT_EXTENDER.get(gid, 'SKSE')
    else:
        _preview          = parse_mo_ini(state.mo_ini_path)
        state.game_name   = _preview.get('game_name',   '')
        state.game_id     = _preview.get('game_id',     '')
        state.respect_esl = _preview.get('respect_esl', False)
        state.se_name     = _preview.get('se_name',     'SKSE')
    gid = state.game_id   # raccourci utilisé pour tous les appels cache

    # ── 1. Cache isolé par jeu ─────────────────────────────────────────────
    emit_status('Lecture du cache…', 'loading')
    state.mod_data    = cache.load_mod_data(gid)
    state.plugin_data = cache.load_plugin_data(gid)
    state.form_ids    = cache.load_form_ids(gid)

    # ── 2. Mod manager ─────────────────────────────────────────────────────
    emit_status('Lecture du mod manager…')
    if state.manager == 'vortex':
        loadorder, mo = load_vortex(
            state.vortex_game_id, state.vortex_profile,
            state.vortex_staging, state.vortex_gamepath,
            state.bsarch_path,
            state.mod_data, state.plugin_data,
            progress_cb=lambda c, t, n, k='mod': _progress_cb(c, t, n, k)
        )
    else:
        loadorder, mo = load_mods(
            state.mo_ini_path, state.bsarch_path,
            state.mod_data, state.plugin_data,
            progress_cb=lambda c, t, n, k='mod': _progress_cb(c, t, n, k)
        )

    state.loadorder    = loadorder
    state.mo_config    = mo
    state.game_modules = mo.get('game_modules', 0)

    # Consolider les infos jeu (peut affiner la détection rapide initiale)
    state.game_name   = mo.get('game_name',   state.game_name)
    state.game_id     = mo.get('game_id',     state.game_id)
    state.se_name     = mo.get('se_name',     state.se_name)
    state.respect_esl = mo.get('respect_esl', state.respect_esl)
    gid = state.game_id

    # Détection MergeMapper.dll (Skyrim SE uniquement en pratique)
    for mdata in state.mod_data.values():
        if mdata.get('_has_mergemapper'):
            state.has_merge_mapper = True
            break

    emit_status('Sauvegarde du cache mods…')
    cache.save_mod_data(state.mod_data, gid)

    # 3. Plugins
    emit_status('Chargement des plugins...')
    plugin_locations = mo.get('plugin_locations', {})
    load_all_plugins(
        loadorder, plugin_locations, state.plugin_data, state.form_ids,
        progress_cb=lambda c, t, n: _progress_cb(c, t, n, 'plugin'),
        skip_hashing=True
    )

    emit_status('Sauvegarde du cache plugins…')
    cache.save_plugin_data(state.plugin_data, gid)
    cache.save_form_ids(state.form_ids, gid)

    # 4. MergeableAnalyzer (avec respect_esl correct selon le jeu)
    state.ma = MergeableAnalyzer(
        state.plugin_data, loadorder,
        respect_esl=state.respect_esl
    )
    state.ma.build_from_data(state.mod_data)
    if state.has_merge_mapper:
        state.ma.enable_merge_mapper()

    # 5. ConflictFinder (pRel)
    emit_status('Calcul des conflits…', 'computing')
    state.cf = ConflictFinder(
        state.plugin_data, state.form_ids,
        loadorder, state.game_modules
    )
    _prel = cache.prel_path(gid)
    prel_loaded = state.cf.load(_prel)
    if not prel_loaded:
        state.cf.compute_all(num_threads=8, progress_cb=_prel_progress_cb)
        state.cf.save(_prel)
    else:
        emit_status('Cache pRel chargé.')

    # 6. GUI
    _rebuild_gui()
    active, total = state.active_count
    emit_status(
        f'Prêt — {state.game_name} · {active} actifs / {total} plugins',
        'ready'
    )
    socketio.emit('ready', _ready_payload())


def _rebuild_gui():
    if state.ma is None:
        return
    state.ma.loadorder = state.loadorder
    state.gui_list     = state.ma.build_gui_list(state.selection)
    state.merge_groups = [row[0] for row in state.gui_list]
    active = sum(1 for row in state.gui_list if row[0] == 0)
    total  = len(state.gui_list)
    state.active_count = (active, total)
    emit_list()
    if state.cf:
        _send_colors()


def _send_colors():
    if state.cf is None or not state.loadorder:
        return
    state.cf.loadorder = state.loadorder
    state.colors = state.cf.get_colors(state.selection)

    mg = state.merge_groups
    mg_conflicts = [False] * (max(mg) + 2 if mg else 1)
    if not (state.sorter and state.sorter.is_running()):
        last_mg  = 0
        conflict = False
        for a in range(len(mg)):
            if mg[a] == 0:
                if last_mg:
                    mg_conflicts[last_mg] = conflict
                last_mg  = 0
                conflict = False
                continue
            last_mg = mg[a]
            if not conflict:
                for b in range(a + 1, len(mg)):
                    if mg[b] == last_mg:
                        rel = state.cf.prel(state.loadorder[a], state.loadorder[b])
                        if rel == 1:
                            conflict = True
                            break
        if last_mg:
            mg_conflicts[last_mg] = conflict
    state.mg_conflicts = mg_conflicts
    emit_colors()


# ---------------------------------------------------------------------------
# Routes Flask
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return send_from_directory(_STATIC, 'index.html')


@app.route('/api/state')
def api_state():
    return jsonify({
        'phase':        state.phase,
        'status':       state.status_msg,
        'loadorder':    state.loadorder,
        'gui_list':     state.gui_list,
        'selection':    state.selection,
        'colors':       state.colors,
        'mg_conflicts': state.mg_conflicts,
        'mo_ini':       state.mo_ini_path,
        'bsarch':       state.bsarch_path,
        'game_name':    state.game_name,
        'game_id':      state.game_id,
        'se_name':      state.se_name,
    })


@app.route('/api/detect_mo2_game', methods=['POST'])
def api_detect_mo2_game():
    """
    Lit rapidement ModOrganizer.ini et retourne les infos du jeu détecté.
    Appelé par la GUI après que l'utilisateur a sélectionné le .ini.
    """
    data = request.json or {}
    path = data.get('path', '').strip()
    if not path or not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'Fichier introuvable'})
    try:
        mo = parse_mo_ini(path)
        return jsonify({
            'ok':        True,
            'game_name': mo.get('game_name', 'Unknown'),
            'game_id':   mo.get('game_id',   ''),
            'se_name':   mo.get('se_name',   'SKSE'),
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/set_paths', methods=['POST'])
def api_set_paths():
    """Configure les chemins pour MO2 ou Vortex."""
    data    = request.json or {}
    manager = data.get('manager', 'mo2')
    state.manager = manager

    if manager == 'vortex':
        state.vortex_game_id  = data.get('game_id',   '')
        state.vortex_profile  = data.get('profile',   '')
        state.vortex_staging  = data.get('staging',   '')
        state.vortex_gamepath = data.get('game_path', '')
    else:
        if 'mo_ini' in data:
            state.mo_ini_path = data['mo_ini']

    if 'bsarch' in data:
        state.bsarch_path = data['bsarch']
    return jsonify({'ok': True})


@app.route('/api/start', methods=['POST'])
def api_start():
    if state.manager == 'vortex':
        if not state.vortex_game_id or not state.vortex_profile:
            return jsonify({'ok': False, 'error': 'Jeu et profil Vortex requis'})
        if not state.vortex_gamepath:
            return jsonify({'ok': False, 'error': 'Chemin du jeu requis'})
    else:
        if not state.mo_ini_path:
            return jsonify({'ok': False, 'error': 'ModOrganizer.ini requis'})
    t = threading.Thread(target=run_pipeline, daemon=True)
    t.start()
    return jsonify({'ok': True})


@app.route('/api/vortex_detect', methods=['GET'])
def api_vortex_detect():
    """Détecte les jeux Bethesda configurés dans Vortex."""
    games = detect_vortex_games()
    return jsonify({'ok': True, 'games': games})


@app.route('/api/vortex_profiles', methods=['GET'])
def api_vortex_profiles():
    """Retourne les profils disponibles pour un jeu Vortex."""
    game_id      = request.args.get('game_id', '')
    vortex_appdata = os.path.join(os.environ.get('APPDATA', ''), 'Vortex')
    profiles_dir   = os.path.join(vortex_appdata, game_id, 'profiles')
    profiles       = detect_vortex_profiles(profiles_dir)
    default_staging = os.path.join(vortex_appdata, game_id, 'mods')
    game_info      = VORTEX_GAME_MAP.get(game_id, ('', '', False))
    return jsonify({
        'ok':              True,
        'profiles':        profiles,
        'default_staging': default_staging,
        'game_name':       game_info[1],
        'se_name':         'F4SE' if 'fallout4' in game_id else
                           'NVSE' if 'falloutnv' in game_id else 'SKSE',
    })


# ---------------------------------------------------------------------------
# Événements WebSocket
# ---------------------------------------------------------------------------

@socketio.on('connect')
def on_connect():
    emit('status', {'msg': state.status_msg, 'phase': state.phase})
    if state.gui_list:
        emit('guilist', {
            'list':      state.gui_list,
            'selection': state.selection,
            'loadorder': state.loadorder,
        })
    if state.colors:
        emit('colors', {
            'colors':      state.colors,
            'mgConflicts': state.mg_conflicts,
        })
    if state.phase == 'ready':
        emit('ready', _ready_payload())


@socketio.on('select')
def on_select(data):
    sel = int(data.get('index', 0))
    if sel < 0 or sel >= len(state.loadorder):
        return
    state.selection = sel
    if state.ma:
        state.ma.selection = sel
    _send_colors()


@socketio.on('toggle_mergeability')
def on_toggle(data):
    sel = int(data.get('index', state.selection))
    if 0 <= sel < len(state.loadorder):
        state.ma.toggle_mergeability(state.loadorder[sel])
        state.selection = sel
        _rebuild_gui()


@socketio.on('ignore_flags')
def on_ignore_flags(data):
    sel = int(data.get('index', state.selection))
    if 0 <= sel < len(state.loadorder):
        state.ma.ignore_flags(state.loadorder[sel])
        _rebuild_gui()


@socketio.on('move_up')
def on_move_up(data):
    sel = int(data.get('index', state.selection))
    if state.cf and state.cf.move_plugin(sel, -1, state.loadorder, force=True):
        state.selection = sel - 1
        _rebuild_gui()


@socketio.on('move_down')
def on_move_down(data):
    sel = int(data.get('index', state.selection))
    if state.cf and state.cf.move_plugin(sel, 1, state.loadorder, force=True):
        state.selection = sel + 1
        _rebuild_gui()


@socketio.on('move_before')
def on_move_before(data):
    sel = int(data.get('index', state.selection))
    if state.cf:
        new_sel, _ = state.cf.move_before(sel, state.merge_groups)
        state.selection = new_sel
        _rebuild_gui()


@socketio.on('move_after')
def on_move_after(data):
    sel = int(data.get('index', state.selection))
    if state.cf:
        new_sel, _ = state.cf.move_after(sel, state.merge_groups)
        state.selection = new_sel
        _rebuild_gui()


@socketio.on('move_to_master')
def on_move_to_master(data):
    sel = int(data.get('index', state.selection))
    if state.cf:
        new_sel, _ = state.cf.move_to_master(sel)
        state.selection = new_sel
        _rebuild_gui()


@socketio.on('move_to_child')
def on_move_to_child(data):
    sel = int(data.get('index', state.selection))
    if state.cf:
        new_sel, _ = state.cf.move_to_child(sel)
        state.selection = new_sel
        _rebuild_gui()


@socketio.on('toggle_autosort')
def on_toggle_autosort(data):
    if state.sorter and state.sorter.is_running():
        best = state.sorter.stop()
        state.loadorder = best
        state.sorter = None
        _rebuild_gui()
        active, total = state.active_count
        emit_status(
            f'Prêt — {state.game_name} · {active} actifs / {total} plugins',
            'ready'
        )
        socketio.emit('ready', _ready_payload())
    else:
        if state.cf and state.ma:
            state.sorter = AutoSorter(
                state.ma, state.cf, state.loadorder, state.game_modules)
            state.sorter.start(update_cb=_autosort_update)
            emit_status('AutoSort en cours...')


def _autosort_update(new_lo: list[str]):
    state.loadorder = new_lo
    _rebuild_gui()


@socketio.on('save_loadorder')
def on_save_loadorder(_data):
    """
    Sauvegarde loadorder.txt et met à jour plugins.txt.
    Fonctionne pour MO2 et Vortex, tous jeux.
    """
    import re as _re

    to_disable = set()
    for i, row in enumerate(state.gui_list):
        if row[0] != 0 and i < len(state.loadorder):
            to_disable.add(state.loadorder[i].lower())

    if state.manager == 'vortex':
        vortex_appdata = os.path.join(os.environ.get('APPDATA', ''), 'Vortex')
        profile_dir    = os.path.join(
            vortex_appdata, state.vortex_game_id,
            'profiles', state.vortex_profile)
        if not os.path.isdir(profile_dir):
            emit('error', {'msg': f'Dossier profil Vortex introuvable : {profile_dir}'})
            return
        lo_path      = os.path.join(profile_dir, 'loadorder.txt')
        plugins_path = os.path.join(profile_dir, 'plugins.txt')
    else:
        modir   = state.mo_config.get('modir', '')
        profile = state.mo_config.get('selected_profile', '')
        if not modir or not profile:
            emit('error', {'msg': 'Chemin MO non configuré'})
            return
        lo_path      = os.path.join(modir, 'profiles', profile, 'loadorder.txt')
        plugins_path = os.path.join(modir, 'profiles', profile, 'plugins.txt')

    try:
        with open(lo_path, 'w', encoding='utf-8') as f:
            for p in state.loadorder:
                f.write(p + '\n')
    except Exception as e:
        emit('error', {'msg': f'loadorder.txt : {e}'})
        return

    try:
        with open(plugins_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()

        new_lines = []
        for line in lines:
            stripped = line.rstrip('\n\r')
            m = _re.match(r'^([*]?)(.*\.es[mpl])$', stripped, _re.I)
            if m:
                star, name = m.group(1), m.group(2)
                if name.lower() in to_disable:
                    new_lines.append(name + '\n')
                else:
                    new_lines.append(stripped + '\n')
            else:
                new_lines.append(line)

        with open(plugins_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)

        manager_label = 'Vortex' if state.manager == 'vortex' else 'MO2'
        emit('info', {'msg': (
            f'[{manager_label} / {state.game_name}] Sauvegardé — '
            f'{len(to_disable)} plugins désactivés (mergés), '
            f'loadorder.txt mis à jour'
        )})
    except Exception as e:
        emit('error', {'msg': f'plugins.txt : {e}'})


@socketio.on('quit')
def on_quit(_data):
    if state.sorter:
        state.sorter.stop()
    socketio.stop()


@app.route('/api/browse', methods=['POST'])
def api_browse():
    """Ouvre un dialogue de fichier/dossier Windows via tkinter."""
    data      = request.json or {}
    file_type = data.get('type', 'ini')
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        if file_type == 'folder':
            path = filedialog.askdirectory(title='Sélectionner un dossier')
        elif file_type == 'ini':
            path = filedialog.askopenfilename(
                title='Sélectionner ModOrganizer.ini',
                filetypes=[('Fichiers INI', '*.ini'), ('Tous', '*.*')]
            )
        else:
            path = filedialog.askopenfilename(
                title='Sélectionner bsarch.exe',
                filetypes=[('Exécutables', '*.exe'), ('Tous', '*.*')]
            )
        root.destroy()
        if path:
            return jsonify({'ok': True, 'path': path.replace('/', '\\')})
        return jsonify({'ok': False, 'path': ''})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/create_merge', methods=['POST'])
def api_create_merge():
    """
    Génère le merges.json compatible zMerge.
    Fonctionne pour Skyrim SE/LE, Fallout 4 et Fallout NV.
    """
    import hashlib as _hashlib
    import re as _re

    if not state.loadorder or not state.gui_list:
        return jsonify({'ok': False, 'error': 'Pas de loadorder chargé'})

    mg    = [row[0] for row in state.gui_list]
    lo    = state.loadorder
    modir = state.mo_config.get('modir', '')

    lo_index = {p: i for i, p in enumerate(lo)}

    groups: dict = {}
    for i, plugin in enumerate(lo):
        g = mg[i]
        if g > 0:
            groups.setdefault(g, []).append(plugin)

    def collect_all_deps(plugin: str, visited: set) -> None:
        if plugin in visited:
            return
        visited.add(plugin)
        for mast in state.plugin_data.get(plugin, {}).get('Masters', []):
            collect_all_deps(mast, visited)

    def md5_of(path: str) -> str:
        h = _hashlib.md5()
        try:
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return '0' * 32

    def get_data_folder(plugin: str) -> str:
        loc = state.plugin_data.get(plugin, {}).get('Location', '')
        if not loc:
            return ''
        norm = loc.replace('/', '\\')
        m = _re.search(r'(.*\\mods\\[^\\]+)\\', norm)
        if m:
            return m.group(1) + '\\'
        return os.path.dirname(norm).rstrip('\\') + '\\'

    out = []
    for mg_num in sorted(groups.keys()):
        plugins_in_group = groups[mg_num]
        n = str(mg_num).zfill(2)

        all_deps: set = set()
        for p in plugins_in_group:
            collect_all_deps(p, all_deps)
        masters_only = all_deps - set(plugins_in_group)

        load_order_list = (
            sorted(masters_only, key=lambda p: lo_index.get(p, -1)) +
            plugins_in_group +
            [f'Smash Merge {n}.esp']
        )

        plugins_list = []
        for p in plugins_in_group:
            pdata = state.plugin_data.get(p, {})
            if pdata.get('BaseGame'):
                continue
            loc    = pdata.get('Location', '')
            folder = get_data_folder(p)
            h      = md5_of(loc) if loc else '0' * 32
            plugins_list.append({
                'filename':   p,
                'dataFolder': folder,
                'hash':       h,
            })

        out.append({
            'name':               f'Merge {n}',
            'filename':           f'Merge {n}.esp',
            'method':             'Clobber',
            'loadOrder':          load_order_list,
            'archiveAction':      'Copy',
            'buildMergedArchive': False,
            'useGameLoadOrder':   False,
            'handleFaceData':     True,
            'handleVoiceData':    True,
            'handleBillboards':   True,
            'handleStringFiles':  True,
            'handleTranslations': True,
            'handleIniFiles':     True,
            'handleDialogViews':  True,
            'copyGeneralAssets':  False,
            'customMetadata':     {},
            'plugins':            plugins_list,
        })

    return jsonify({'ok': True, 'data': out})


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------

def run(host: str = '127.0.0.1', port: int = 5000,
        open_browser: bool = True) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s'
    )
    if open_browser:
        def _open():
            time.sleep(1.5)
            webbrowser.open(f'http://{host}:{port}')
        threading.Thread(target=_open, daemon=True).start()

    socketio.run(
        app, host=host, port=port,
        debug=False, use_reloader=False,
        allow_unsafe_werkzeug=True
    )