"""
cache.py
Gestion des fichiers de cache JSON, isolés par jeu.
Chaque jeu obtient son propre sous-dossier  cache/<game_id>/
pour éviter que les données Skyrim n'écrasent celles de Fallout 4, etc.
"""

import json
import os
import logging

log = logging.getLogger(__name__)

MOD_DATA_FILE    = 'modData.json'
PLUGIN_DATA_FILE = 'pluginData.json'
FORM_IDS_FILE    = 'formIDs.json'
PREL_FILE        = 'pRel.json'


def _cache_path(filename: str, game_id: str = '') -> str:
    """
    Retourne le chemin complet du fichier de cache.
    Si game_id est fourni, le fichier est stocké dans cache/<game_id>/.
    Le dossier est créé automatiquement si nécessaire.
    """
    if game_id:
        d = os.path.join('cache', game_id)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, filename)
    return filename


def load_json(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'rb') as f:
            data = f.read()
        return json.loads(data)
    except Exception as e:
        log.warning(f'Cannot load {path}: {e}')
        return {}


def save_json(path: str, data, pretty: bool = False) -> bool:
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            if pretty:
                json.dump(data, f, indent=2, ensure_ascii=False)
            else:
                json.dump(data, f, ensure_ascii=False)
        return True
    except Exception as e:
        log.error(f'Cannot save {path}: {e}')
        return False


def load_mod_data(game_id: str = '')    -> dict:
    return load_json(_cache_path(MOD_DATA_FILE,    game_id))

def load_plugin_data(game_id: str = '') -> dict:
    return load_json(_cache_path(PLUGIN_DATA_FILE, game_id))

def load_form_ids(game_id: str = '')    -> dict:
    return load_json(_cache_path(FORM_IDS_FILE,    game_id))

def load_prel(game_id: str = '')        -> dict:
    return load_json(_cache_path(PREL_FILE,        game_id))


def save_mod_data(d, game_id: str = '')    -> bool:
    return save_json(_cache_path(MOD_DATA_FILE,    game_id), d, pretty=True)

def save_plugin_data(d, game_id: str = '') -> bool:
    return save_json(_cache_path(PLUGIN_DATA_FILE, game_id), d, pretty=True)

def save_form_ids(d, game_id: str = '')    -> bool:
    return save_json(_cache_path(FORM_IDS_FILE,    game_id), d)

def save_prel(d, game_id: str = '')        -> bool:
    return save_json(_cache_path(PREL_FILE,        game_id), d)

def prel_path(game_id: str = '') -> str:
    """Retourne le chemin du fichier pRel.json (pour ConflictFinder.save/load)."""
    return _cache_path(PREL_FILE, game_id)
