"""
auto_sort.py
Équivalent de autoSort.pl — Version Multiprocessing Optimisée (Bypasse le GIL)

Algorithme de tri stochastique (hill-climbing) parallélisé sur tous les cœurs CPU.
Utilise des structures de données extraites et sérialisables pour éviter de cloner
le cache lourd (form_ids), maintenant une consommation RAM infime.
"""

import multiprocessing
import threading
import random
import time
import logging
import os
import sys
from typing import Optional, Callable

log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Fonctions statiques globales (exécutées isolément dans les processus cibles)
# ------------------------------------------------------------------

def _static_faster_merge_groups(lo: list[str], mergeable_values: dict[str, int], children_map: dict[str, list[str]]) -> list[int]:
    """Calcule rapidement les groupes de merge sans dépendre des objets lourds."""
    n = len(lo)
    results = [0] * n
    merge_count = 0
    in_merge = {}
    can_take_navi = True

    for z in range(n - 1, -1, -1):
        mod = lo[z]
        m = mergeable_values.get(mod, 0)

        if m < 0:
            children = children_map.get(mod, [])
            if all(c in in_merge for c in children):
                m = -m
            else:
                m = 0

        if m == 2:
            if can_take_navi:
                m = 1
                can_take_navi = False
            else:
                m = 0

        if m == 0:
            if len(in_merge) == 1:
                merge_count -= 1
            in_merge = {}
            can_take_navi = True

        if m == 1:
            if not in_merge:
                merge_count += 1
            if len(in_merge) > 150:
                in_merge = {}
                merge_count += 1
                can_take_navi = True
            in_merge[mod] = True

        results[z] = merge_count if m else 0

    if merge_count > 0:
        for a in range(n):
            if results[a] != 0:
                results[a] = merge_count + 1 - results[a]

    return results


def _static_count_active(lo: list[str], mergeable_values: dict[str, int], children_map: dict[str, list[str]]) -> float:
    """Évalue le score d'une configuration (recherche du score le plus bas)."""
    mg = _static_faster_merge_groups(lo, mergeable_values, children_map)
    active = 0
    current_merge_size = 0
    current_merge = 0

    for a in range(len(mg)):
        if mg[a] == 0:
            active += 1
            current_merge_size = 0
        else:
            if current_merge < mg[a]:
                current_merge = mg[a]
                active += 1
            current_merge_size += 1

    return active + current_merge / 1000


def _mp_worker(worker_id: int, stop_event, input_pipe, output_queue,
               lo_init: list[str], lo_score_init: float, game_modules: int,
               relations: dict, mergeable_values: dict, children_map: dict) -> None:
    """Boucle principale exécutée sur un cœur CPU dédié."""
    lo = list(lo_init)
    lo_score = lo_score_init
    
    loops_without_improvement = 0
    current_tries = 1
    iteration_counter = 0

    while not stop_event.is_set():
        iteration_counter += 1
        
        # Toutes les 200 itérations, vérifier si un autre cœur a trouvé un meilleur loadorder global
        if iteration_counter % 200 == 0:
            while input_pipe.poll():
                new_global_lo, new_global_score = input_pipe.recv()
                if new_global_score < lo_score:
                    lo = list(new_global_lo)
                    lo_score = new_global_score
                    loops_without_improvement = 0
                    current_tries = 1

        rand_tries = 1 + random.randint(0, current_tries - 1)

        while not stop_event.is_set() and rand_tries > 0:
            rand_tries -= 1
            n = len(lo)
            num = random.randint(game_modules, n - 1)
            direction = random.choice((-1, 1))
            dist = random.randint(0, (n - num - 1) if direction > 0 else (num - game_modules))

            # Tenter le déplacement aléatoire du plugin
            moved = 0
            while not stop_event.is_set() and moved <= dist:
                target = num + direction
                if target < game_modules or target >= n:
                    break
                
                # Vérification rapide des relations structurelles directes (pRel)
                rel = relations.get(lo[num], {}).get(lo[target], 0)
                if rel in (2, 3):  # Relation Parent/Enfant stricte -> interdit
                    break
                    
                lo[num], lo[target] = lo[target], lo[num]
                num += direction
                moved += 1

            # Cohérence locale : reculer tant que l'état de mergeabilité diffère
            my_state = mergeable_values.get(lo[num], 0)
            while (not stop_event.is_set() and 0 <= num - direction < n
                   and my_state != mergeable_values.get(lo[num - direction], 0)):
                target = num - direction
                rel = relations.get(lo[num], {}).get(lo[target], 0)
                if rel in (2, 3):
                    break
                lo[num], lo[target] = lo[target], lo[num]
                num -= direction

            # Évaluation de la mutation
            new_score = _static_count_active(lo, mergeable_values, children_map)
            if new_score < lo_score:
                lo_score = new_score
                loops_without_improvement = 0
                current_tries = 1
                # Envoyer immédiatement la découverte au processus central
                output_queue.put((lo, lo_score))

        if loops_without_improvement > 10 * current_tries:
            loops_without_improvement = 0
            current_tries += 1
        else:
            loops_without_improvement += 1


# ------------------------------------------------------------------
# Classe d'interface principale
# ------------------------------------------------------------------

class AutoSorter:
    """Gère l'orchestration du tri automatique stochastique multi-processus."""

    NUM_WORKER_THREADS = 16
    UPDATE_INTERVAL    = 5

    def __init__(self, mergeable_analyzer, conflict_finder,
                 loadorder: list[str], game_modules: int = 0):
        self.analyzer = mergeable_analyzer
        self.cf = conflict_finder
        self.game_modules = game_modules

        self._lock = threading.Lock()
        self._best: list[str] = list(loadorder)
        self._best_score: float = self.analyzer.count_active(self._best)

        self._sorting = False
        self._worker_processes: list[multiprocessing.Process] = []
        self._worker_pipes: list[multiprocessing.connection.Connection] = []
        self._manager_thread: Optional[threading.Thread] = None
        self._broadcaster_thread: Optional[threading.Thread] = None
        self._update_cb: Optional[Callable[[list[str]], None]] = None

    def start(self, update_cb: Optional[Callable[[list[str]], None]] = None) -> None:
        """Prépare les données légères et propage l'exécution sur tous les cœurs."""
        if self._sorting:
            return
        self._sorting = True
        self._update_cb = update_cb

        # 1. Extraction des données strictement requises (Léger & Sérialisable)
        mergeable_values = {p: self.analyzer.is_mergeable(p) for p in self._best}
        children_map = {p: self.analyzer.plugin_data.get(p, {}).get('Children', []) for p in self._best}
        relations_copy = {k: dict(v) for k, v in self.cf.relations.items()}

        self._stop_event = multiprocessing.Event()
        self._output_queue = multiprocessing.Queue()
        self._worker_processes = []
        self._worker_pipes = []

        # Utiliser le nombre de cœurs réels disponibles (limité à la constante)
        num_workers = min(self.NUM_WORKER_THREADS, os.cpu_count() or 4)

        # 2. Instanciation des sous-processus de calcul brut
        for worker_id in range(num_workers):
            parent_pipe, child_pipe = multiprocessing.Pipe()
            p = multiprocessing.Process(
                target=_mp_worker,
                args=(
                    worker_id,
                    self._stop_event,
                    child_pipe,
                    self._output_queue,
                    list(self._best),
                    self._best_score,
                    self.game_modules,
                    relations_copy,
                    mergeable_values,
                    children_map
                ),
                daemon=True
            )
            p.start()
            self._worker_processes.append(p)
            self._worker_pipes.append(parent_pipe)

        # 3. Lancement du gestionnaire central d'écoute (Thread asynchrone)
        self._manager_thread = threading.Thread(target=self._master_manager, daemon=True)
        self._manager_thread.start()

        # 4. Thread de diffusion UI périodique
        self._broadcaster_thread = threading.Thread(target=self._broadcaster, daemon=True)
        self._broadcaster_thread.start()

        log.info(f'AutoSort démarré avec {num_workers} cœurs processeurs indépendants.')

    def _master_manager(self) -> None:
        """Récupère les optimisations trouvées par les cœurs et les synchronise."""
        while self._sorting:
            try:
                item = self._output_queue.get(timeout=1.0)
                if item is None:
                    continue
                lo, score = item

                with self._lock:
                    if score < self._best_score:
                        self._best_score = score
                        self._best = list(lo)

                        # Renvoyer immédiatement le nouveau record à TOUS les autres processus
                        for pipe in self._worker_pipes:
                            try:
                                pipe.send((self._best, self._best_score))
                            except Exception:
                                pass
            except Exception:
                continue

    def stop(self) -> list[str]:
        """Arrête l'ensemble des processus de calcul et renvoie le loadorder optimal."""
        if not self._sorting:
            return list(self._best)
        self._sorting = False

        if hasattr(self, '_stop_event'):
            self._stop_event.set()

        for p in self._worker_processes:
            p.join(timeout=1)
            if p.is_alive():
                p.terminate()

        if self._manager_thread:
            self._manager_thread.join(timeout=1)
        if self._broadcaster_thread:
            self._broadcaster_thread.join(timeout=1)

        self._worker_processes = []
        self._worker_pipes = []

        log.info(f'AutoSort arrêté. Meilleur score final : {self._best_score:.3f}')
        return list(self._best)

    def is_running(self) -> bool:
        return self._sorting

    def get_best(self) -> tuple[list[str], float]:
        with self._lock:
            return list(self._best), self._best_score

    def _broadcaster(self) -> None:
        """Envoie les mises à jour à l'interface utilisateur à intervalles réguliers."""
        last_sent: Optional[list[str]] = None
        while self._sorting:
            time.sleep(self.UPDATE_INTERVAL)
            if not self._sorting:
                break
            with self._lock:
                best = list(self._best)
            if best != last_sent:
                last_sent = best
                self.cf.loadorder = best
                self.analyzer.loadorder = best
                if self._update_cb:
                    try:
                        self._update_cb(best)
                    except Exception as e:
                        log.error(f'AutoSort update callback error: {e}')