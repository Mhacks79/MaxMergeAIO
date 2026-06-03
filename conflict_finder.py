"""
conflict_finder.py
Équivalent de conflictFinder.pl + pRel.pl fusionnés.

Calcule la matrice de relations entre tous les plugins (pRel) :
  0 = Sans relation (ou même plugin)
  1 = Conflit (le plugin B écrase des records du plugin A)
  2 = B est un enfant de A (A est master de B)
  3 = B est un master de A
  4 = inverse de 1 (A écrase B — utilisé dans autoSort)
  5 = inverse de 2/3

Le calcul se fait en multithread pour aller vite.
La matrice est sauvegardée dans pRel.json.
"""

import json
import logging
import os
import threading
from typing import Optional, Callable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_master_of(mod_a: str, mod_b: str, plugin_data: dict,
                  _cache: dict = {}) -> bool:
    """Retourne True si mod_b est un master (direct ou indirect) de mod_a."""
    key = (mod_a, mod_b)
    if key in _cache:
        return _cache[key]
    visited = set()
    stack = list(plugin_data.get(mod_a, {}).get('Masters', []))
    while stack:
        mast = stack.pop()
        if mast in visited:
            continue
        visited.add(mast)
        if mast == mod_b:
            _cache[key] = True
            return True
        stack.extend(plugin_data.get(mast, {}).get('Masters', []))
    _cache[key] = False
    return False


def _have_conflicts(mod_a: str, mod_b: str,
                    form_ids: dict, plugin_data: dict,
                    conflicts_cache: dict, lock: threading.Lock) -> bool:
    """
    Retourne True si mod_a et mod_b ont des records conflictuels
    (même origin, même type+formID, mais MD5 différent).
    """
    with lock:
        if mod_a in conflicts_cache and mod_b in conflicts_cache[mod_a]:
            return conflicts_cache[mod_a][mod_b]

    # Pas de conflit possible si l'un est master de l'autre
    if _is_master_of(mod_a, mod_b, plugin_data) or \
       _is_master_of(mod_b, mod_a, plugin_data):
        result = False
    else:
        result = False
        forms_a = form_ids.get(mod_a, {})
        forms_b = form_ids.get(mod_b, {})
        for origin, records_a in forms_a.items():
            if origin not in forms_b:
                continue
            records_b = forms_b[origin]
            for record, hash_a in records_a.items():
                if record in records_b and records_b[record] != hash_a:
                    result = True
                    break
            if result:
                break

    with lock:
        conflicts_cache.setdefault(mod_a, {})[mod_b] = result
        conflicts_cache.setdefault(mod_b, {})[mod_a] = result
    return result


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class ConflictFinder:
    """
    Calcule et stocke la matrice pRel.
    Équivalent de conflictFinder.pl (calcul initial, multithread)
    et pRel.pl (lecture du cache + navigation).
    """

    PREL_FILE = 'pRel.json'

    def __init__(self, plugin_data: dict, form_ids: dict,
                 loadorder: list[str], game_modules: int = 0):
        self.plugin_data   = plugin_data
        self.form_ids      = form_ids
        self.loadorder     = loadorder
        self.game_modules  = game_modules

        # La matrice : relations[A][B] = pRel(A, B)
        self.relations: dict[str, dict[str, int]] = {}
        self._rel_lock = threading.Lock()

        # Cache de conflits
        self._conflicts_cache: dict = {}
        self._conf_lock = threading.Lock()

        self._is_master_cache: dict = {}

        # Pour le suivi de progression
        self._progress_cb: Optional[Callable] = None

    # ------------------------------------------------------------------
    # Relation entre deux plugins
    # ------------------------------------------------------------------

    def prel(self, mod_a: str, mod_b: str) -> int:
        """
        Retourne la relation de mod_b par rapport à mod_a.
        Calcule et mémorise si pas encore connu.
        """
        with self._rel_lock:
            if mod_a in self.relations and mod_b in self.relations[mod_a]:
                return self.relations[mod_a][mod_b]

        value = self._compute_prel(mod_a, mod_b)

        # Valeur inverse
        inverse = value
        if value in (2, 3):
            inverse = 5 - value  # 2↔3

        with self._rel_lock:
            self.relations.setdefault(mod_a, {})[mod_b] = value
            self.relations.setdefault(mod_b, {})[mod_a] = inverse

        return value

    def _compute_prel(self, mod_a: str, mod_b: str) -> int:
        if mod_a == mod_b:
            return 0
        if self.loadorder and mod_a == self.loadorder[0]:
            return 2  # Tous les mods sont enfants du premier

        # Vérifier si déjà calculé depuis l'autre sens
        with self._rel_lock:
            if mod_b in self.relations and mod_a in self.relations[mod_b]:
                inv = self.relations[mod_b][mod_a]
                return (5 - inv) if inv in (2, 3) else inv

        # Calcul réel
        if self._is_master(mod_a, mod_b):
            return 3  # B est master de A
        if self._is_master(mod_b, mod_a):
            return 2  # B est enfant de A
        if _have_conflicts(mod_a, mod_b, self.form_ids, self.plugin_data,
                           self._conflicts_cache, self._conf_lock):
            return 1  # Conflictuel
        return 0

    def _is_master(self, mod_a: str, mod_b: str) -> bool:
        key = (mod_a, mod_b)
        if key not in self._is_master_cache:
            self._is_master_cache[key] = _is_master_of(
                mod_a, mod_b, self.plugin_data)
        return self._is_master_cache[key]

    # ------------------------------------------------------------------
    # Calcul complet de la matrice (multithread)
    # ------------------------------------------------------------------

    def compute_all(self, num_threads: int = 8,
                    progress_cb: Optional[Callable] = None) -> None:
        """
        Calcule pRel pour toutes les paires de plugins.
        progress_cb(percent: int) est appelé régulièrement.
        Équivalent du threadWork dans conflictFinder.pl
        """
        lo = self.loadorder
        n  = len(lo)
        if n == 0:
            return

        # Supprimer les conflits non-gagnants avant le calcul
        # (idem au bloc dans loadCache() de conflictFinder.pl)
        self._prune_non_winning_conflicts()

        # Générer toutes les paires à calculer
        # On part du bas de la liste (les derniers mods écrasent les premiers)
        pairs: list[tuple[int, int]] = []
        for a in range(n):
            for b in range(a, n):
                pairs.append((a, b))

        total   = len(pairs)
        done    = [0]
        lock    = threading.Lock()

        def worker(chunk):
            for a_idx, b_idx in chunk:
                self.prel(lo[a_idx], lo[b_idx])
                with lock:
                    done[0] += 1
                    if progress_cb and done[0] % max(1, total // 100) == 0:
                        progress_cb(int(100 * done[0] / total))

        # Découper les paires en chunks pour les threads
        chunk_size = max(1, total // num_threads)
        threads = []
        for i in range(0, total, chunk_size):
            chunk = pairs[i:i + chunk_size]
            t = threading.Thread(target=worker, args=(chunk,), daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        if progress_cb:
            progress_cb(100)

    def _prune_non_winning_conflicts(self) -> None:
        """
        Supprime les conflits non-gagnants des formIDs.
        (Un conflit non-gagnant = un record écrasé par un mod encore plus tardif.)
        Équivalent du bloc de nettoyage dans loadCache() de conflictFinder.pl
        """
        lo = self.loadorder
        for a in range(len(lo) - 1, 0, -1):
            forms_a = self.form_ids.get(lo[a], {})
            for master, records_a in forms_a.items():
                for b in range(a):
                    forms_b = self.form_ids.get(lo[b], {})
                    if master not in forms_b:
                        continue
                    for record in list(records_a.keys()):
                        if record in forms_b[master]:
                            del forms_b[master][record]

    # ------------------------------------------------------------------
    # Persistance
    # ------------------------------------------------------------------

    def save(self, path: str = PREL_FILE) -> bool:
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.relations, f)
            return True
        except Exception as e:
            log.error(f'Cannot save pRel.json: {e}')
            return False

    def load(self, path: str = PREL_FILE) -> bool:
        if not os.path.isfile(path):
            return False
        try:
            with open(path, 'rb') as f:
                data = f.read()
            self.relations = json.loads(data)
            return True
        except Exception as e:
            log.error(f'Cannot load pRel.json: {e}')
            return False

    # ------------------------------------------------------------------
    # Résultats pour la GUI (coloration)
    # ------------------------------------------------------------------

    def get_colors(self, base_idx: int) -> list[int]:
        """
        Retourne la liste des relations de tous les plugins par rapport
        au plugin sélectionné (base_idx).
        Utilisé pour colorier la liste dans la GUI.
        """
        lo = self.loadorder
        if base_idx < 0 or base_idx >= len(lo):
            return [0] * len(lo)
        base = lo[base_idx]
        return [self.prel(base, lo[i]) for i in range(len(lo))]

    # ------------------------------------------------------------------
    # Navigation (move)
    # ------------------------------------------------------------------

    def can_move(self, num: int, direction: int,
                 lo: Optional[list[str]] = None,
                 force: bool = False) -> bool:
        """
        Vérifie si on peut déplacer le plugin à la position num
        dans la direction donnée (-1 = up, +1 = down).
        """
        lo = lo if lo is not None else self.loadorder
        target = num + direction
        if not (direction in (-1, 1)):
            return False
        if target < self.game_modules or target >= len(lo):
            return False
        rel = self.prel(lo[num], lo[target])
        if rel in (0, 5):
            return True
        if force and rel in (1, 4):
            return True
        return False

    def move_plugin(self, num: int, direction: int,
                    lo: Optional[list[str]] = None,
                    force: bool = False) -> bool:
        """
        Déplace le plugin à la position num dans la direction donnée.
        Retourne True si le déplacement a eu lieu.
        """
        lo = lo if lo is not None else self.loadorder
        if not self.can_move(num, direction, lo, force):
            return False
        target = num + direction
        lo[num], lo[target] = lo[target], lo[num]
        return True

    def move_before(self, position: int,
                    merge_groups: list[int]) -> tuple[int, list[str]]:
        """Déplace le plugin avant son groupe de merge précédent."""
        lo = list(self.loadorder)
        if position == 0:
            return position, lo
        group_state = merge_groups[position - 1] if position > 0 else 0
        while position > 0 and merge_groups[position - 1] == group_state:
            if not self.move_plugin(position, -1, lo):
                break
            position -= 1
        self.loadorder = lo
        return position, lo

    def move_after(self, position: int,
                   merge_groups: list[int]) -> tuple[int, list[str]]:
        """Déplace le plugin après son groupe de merge suivant."""
        lo = list(self.loadorder)
        if position >= len(lo) - 1:
            return position, lo
        group_state = merge_groups[position + 1] if position + 1 < len(merge_groups) else 0
        while position + 1 < len(lo) and merge_groups[position + 1] == group_state:
            if not self.move_plugin(position, 1, lo):
                break
            position += 1
        self.loadorder = lo
        return position, lo

    def move_to_master(self, position: int) -> tuple[int, list[str]]:
        """Déplace le plugin aussi loin que possible vers le haut."""
        lo = list(self.loadorder)
        while self.move_plugin(position, -1, lo):
            position -= 1
        self.loadorder = lo
        return position, lo

    def move_to_child(self, position: int) -> tuple[int, list[str]]:
        """Déplace le plugin aussi loin que possible vers le bas."""
        lo = list(self.loadorder)
        while self.move_plugin(position, 1, lo):
            position += 1
        self.loadorder = lo
        return position, lo
