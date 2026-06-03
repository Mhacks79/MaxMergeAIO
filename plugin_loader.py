"""
plugin_loader.py
Lit les fichiers .esp/.esm/.esl de Bethesda pour extraire :
  - Masters (dépendances)
  - Children (plugins qui ont ce plugin comme master)
  - Flags ESM / ESL / Localized / NAVI
  - FormIDs de chaque record (pour la détection de conflits)
  - Hash MD5 pour savoir si le fichier a changé depuis le dernier cache
"""

import struct
import hashlib
import io
import json
import os
import logging
from typing import Optional

log = logging.getLogger(__name__)

# Types de records dont les conflits ne comptent pas
IGNORED_TYPES = {
    'DNAM','TFIC','PNAM','ANAM','ACPR','LCPR','RCUN','ACSR','LCSR','RCSR',
    'ACEC','LCEC','RCEC','ACID','LCID','ACEP','LCEP','SNAM','QNAM',
}


def _read_exact(f, n: int) -> Optional[bytes]:
    data = f.read(n)
    if len(data) != n:
        return None
    return data


def _u32(data: bytes) -> int:
    return struct.unpack_from('<I', data)[0]


def _u16(data: bytes) -> int:
    return struct.unpack_from('<H', data)[0]


def _fix_plugin_capitalization(name: str, loadorder: list[str]) -> str:
    nl = name.lower()
    for p in loadorder:
        if p.lower() == nl:
            return p
    return name


def _parse_group(buf: bytes, plugin_info: dict, form_ids: dict,
                 plugin_data: dict, loadorder: list[str],
                 total_records: list, read_records: list):
    """Parse un GRUP Bethesda depuis un buffer bytes."""
    view = io.BytesIO(buf)
    size = len(buf)
    while view.tell() < size:
        _parse_record(view, plugin_info, form_ids, plugin_data,
                      loadorder, total_records, read_records)


def _parse_record(f, plugin_info: dict, form_ids: dict,
                  plugin_data_all: dict, loadorder: list[str],
                  total_records: list, read_records: list):
    """
    Parse un record ou un GRUP depuis le fichier ouvert f.
    plugin_info : entrée pluginData pour CE plugin.
    form_ids    : entrée formIDs pour CE plugin  { origin -> { 'TYPE formid' -> md5 } }
    """
    read_records[0] += 1

    type_b = f.read(4)
    if not type_b or len(type_b) < 4:
        return

    rec_type = type_b.decode('latin-1')

    if rec_type == 'GRUP':
        raw_size = f.read(4)
        if not raw_size or len(raw_size) < 4:
            return
        size = _u32(raw_size)
        # label(4) + grouptype(4) + timestamp(2) + version(2) + unknown(4) = 16 bytes
        header_rest = f.read(16)
        if not header_rest or len(header_rest) < 16:
            return
        body_size = size - 24  # 4(GRUP) + 4(size) + 16(header) = 24
        body = f.read(body_size)
        if body:
            _parse_group(body, plugin_info, form_ids, plugin_data_all,
                         loadorder, total_records, read_records)
        return

    # Record normal
    raw_size = f.read(4)
    if not raw_size or len(raw_size) < 4:
        return
    size = _u32(raw_size)

    raw_flags = f.read(4)
    flags = _u32(raw_flags) if raw_flags and len(raw_flags) == 4 else 0

    raw_form = f.read(4)
    # FormID stocké big-endian dans le fichier → on reverse pour avoir hex lisible
    form_id_hex = raw_form[::-1].hex() if raw_form and len(raw_form) == 4 else '00000000'

    f.read(2)  # timestamp
    f.read(2)  # version
    f.read(2)  # intversion
    f.read(2)  # unknown

    data = f.read(size)
    if data is None or len(data) < size:
        data = data or b''

    plugin_name = plugin_info['Name']
    plugin_info.setdefault('RecordTypes', set())
    plugin_info['RecordTypes'].add(rec_type)

    if rec_type == 'TES4':
        if flags & 0x00000001:
            plugin_info['ESM'] = True
        if flags & 0x00000200:
            plugin_info['ESL'] = True
        if flags & 0x00000080:
            plugin_info['Localized'] = True
        _parse_fields_tes4(data, plugin_info, plugin_data_all,
                           loadorder, total_records)

    elif rec_type == 'NAVI':
        plugin_info['NAVI'] = True

    else:
        if rec_type not in IGNORED_TYPES:
            # Déterminer l'origine du record
            origin = plugin_name
            if form_id_hex[:2] != '00':
                plugin_info['InjectedRecords'] = plugin_info.get('InjectedRecords',0)+1
            else:
                plugin_info['NewRecords'] = plugin_info.get('NewRecords',0)+1
            masters = plugin_info.get('Masters', [])
            for mast in masters:
                mast_forms = form_ids.get(mast, {})
                for o, records in mast_forms.items():
                    key = rec_type + ' ' + form_id_hex
                    if key in records:
                        # Ce record vient d'un master dont on est l'enfant
                        if _is_master_of(plugin_name, o, plugin_data_all):
                            origin = o
                            break

            if plugin_name not in form_ids:
                form_ids[plugin_name] = {}
            if origin not in form_ids[plugin_name]:
                form_ids[plugin_name][origin] = {}

            key = rec_type + ' ' + form_id_hex
            form_ids[plugin_name][origin][key] = hashlib.md5(data).hexdigest()


def _parse_fields_tes4(data: bytes, plugin_info: dict, plugin_data_all: dict,
                       loadorder: list[str], total_records: list):
    """Parse les sous-champs du record TES4 (header du plugin)."""
    pos = 0
    while pos < len(data):
        if pos + 6 > len(data):
            break
        field_type = data[pos:pos+4].decode('latin-1')
        field_size = _u16(data[pos+4:pos+6])
        pos += 6
        if field_size == 0:
            continue
        if pos + field_size > len(data):
            break
        field_data = data[pos:pos+field_size]
        pos += field_size

        if field_type == 'HEDR':
            # float(4) + num_records(4) + next_object_id(4)
            if len(field_data) >= 8:
                total_records[0] = _u32(field_data[4:8])

        elif field_type == 'MAST':
            # Nom du master, null-terminated
            master_name_raw = field_data.rstrip(b'\x00').decode('latin-1')
            master_name = _fix_plugin_capitalization(master_name_raw, loadorder)

            # Vérifier que le master existe dans nos données
            if master_name not in plugin_data_all or not plugin_data_all[master_name].get('Exists'):
                plugin_info['Exists'] = False
                plugin_info['FailureReason'] = f'Missing master: {master_name}'
                log.warning(f"{plugin_info['Name']} has a missing master: {master_name}")
            else:
                plugin_info.setdefault('Masters', [])
                plugin_info['Masters'].append(master_name)


def _is_master_of(plugin_a: str, plugin_b: str, plugin_data: dict) -> bool:
    """Retourne True si plugin_b est un master (direct ou indirect) de plugin_a."""
    visited = set()
    stack = list(plugin_data.get(plugin_a, {}).get('Masters', []))
    while stack:
        mast = stack.pop()
        if mast in visited:
            continue
        visited.add(mast)
        if mast == plugin_b:
            return True
        stack.extend(plugin_data.get(mast, {}).get('Masters', []))
    return False


def md5_file(path: str) -> str:
    h = hashlib.md5()
    try:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
    except OSError:
        return ''
    return h.hexdigest()


def read_plugin(plugin_name: str, location: str, plugin_data_all: dict,
                form_ids: dict, loadorder: list[str],
                skip_hashing: bool = True) -> None:
    """
    Lit un plugin Bethesda et remplit plugin_data_all[plugin_name] et form_ids[plugin_name].
    Équivalent de la sub readPlugin() dans pluginLoader.pl
    """
    entry = plugin_data_all.setdefault(plugin_name, {})
    entry['Name']          = plugin_name
    entry['Location']      = location
    entry['Masters']       = []
    entry['Children']      = []
    entry['ESM']           = False
    entry['ESL']           = False
    entry['Localized']     = False
    entry['NAVI']          = False
    entry['Exists']        = False
    entry['FailureReason'] = f'Unable to open: {location}'

    if not os.path.isfile(location):
        log.warning(f'File not found: {location}')
        return

    md5 = '' if skip_hashing else md5_file(location)
    entry['MD5'] = md5

    # Si le cache est valide, inutile de relire
    if entry.get('Exists') and (skip_hashing or (md5 and md5 == entry.get('MD5', ''))):
        return

    try:
        with open(location, 'rb') as f:
            if not md5:
                md5 = md5_file(location)
                entry['MD5'] = md5
                f.seek(0)

            entry['FailureReason'] = ''
            total_records = [0]
            read_records  = [0]
            file_size = os.path.getsize(location)

            while read_records[0] <= total_records[0] and f.tell() < file_size:
                _parse_record(f, entry, form_ids, plugin_data_all,
                               loadorder, total_records, read_records)
                # Arrêt si on a lu le TES4 mais que total_records est toujours 0
                # (petit fichier ou header uniquement)
                if read_records[0] >= 1 and total_records[0] == 0:
                    break

    except Exception as e:
        entry['FailureReason'] = str(e)
        log.error(f'Error reading {plugin_name}: {e}')
        return

    if not entry.get('FailureReason'):
        entry['Exists'] = True
        # Ajouter ce plugin comme enfant de chacun de ses masters
        for mast in entry.get('Masters', []):
            if mast in plugin_data_all:
                plugin_data_all[mast].setdefault('Children', [])
                if plugin_name not in plugin_data_all[mast]['Children']:
                    plugin_data_all[mast]['Children'].append(plugin_name)


def load_all_plugins(loadorder: list[str], plugin_locations: dict,
                     plugin_data: dict, form_ids: dict,
                     progress_cb=None, skip_hashing: bool = True) -> None:
    """
    Charge tous les plugins du loadorder.
    Équivalent du handler LOADPLUGINS dans pluginLoader.pl
    progress_cb(current, total, name) → appelé à chaque plugin
    """
    total = len(loadorder)
    for i, plugin_name in enumerate(loadorder):
        if progress_cb:
            progress_cb(i + 1, total, plugin_name)

        location = plugin_locations.get(plugin_name, '')
        if not location:
            location = plugin_data.get(plugin_name, {}).get('Location', '')

        # Vérifier si on peut sauter (cache valide)
        cached = plugin_data.get(plugin_name, {})
        if cached.get('Exists') and skip_hashing:
            # Toujours s'assurer que les Children sont bien initialisés
            plugin_data[plugin_name].setdefault('Children', [])
            continue

        read_plugin(plugin_name, location, plugin_data, form_ids,
                    loadorder, skip_hashing)

    # Deuxième passe : reconstruire tous les liens Children
    # (au cas où le cache partiel aurait des orphelins)
    for plugin_name, entry in plugin_data.items():
        if not entry.get('Exists'):
            continue
        for mast in entry.get('Masters', []):
            if mast in plugin_data:
                plugin_data[mast].setdefault('Children', [])
                if plugin_name not in plugin_data[mast]['Children']:
                    plugin_data[mast]['Children'].append(plugin_name)
