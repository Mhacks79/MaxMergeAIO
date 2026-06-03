"""
mergeable.py
Équivalent de mergeable.pl

Détermine si chaque plugin est mergeable (sans conflit, sans dépendance bloquante)
et calcule les groupes de merge (merge groups).

Valeurs de retour de is_mergeable() :
   0  → non mergeable
   1  → mergeable
  -1  → mergeable mais a des enfants mergeables (merge group parent)
   2  → mergeable mais contient NAVI (une seule par groupe)

Supporte Skyrim SE/LE/VR, Enderal, Fallout 4, Fallout New Vegas.
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Issues ignorées par défaut (ne bloquent PAS la mergeabilité)
# ESL et ESM sont des flags techniques natifs, pas des problèmes de merge.
DEFAULT_IGNORED = {'ESL'}


class MergeableAnalyzer:
    """
    Calcule la mergeabilité des plugins et les groupes de merge.
    Fonctionne pour tous les jeux Bethesda (Skyrim, Fallout 4, Fallout NV, etc.).
    """

    MERGE_GROUP_LIMIT = 250

    def __init__(self, plugin_data: dict, loadorder: list[str],
                 respect_esl: bool = True):
        self.plugin_data  = plugin_data
        self.loadorder    = loadorder
        self.mergeable: dict[str, dict[str, bool]] = {}
        self.manual_decision: dict[str, Optional[bool]] = {}

        ignored = set(DEFAULT_IGNORED)
        if not respect_esl:
            ignored.add('ESL')   # LE / FNV : pas d'ESL natif
        self.ignored_issues: dict[str, bool] = dict.fromkeys(ignored, True)

    # ------------------------------------------------------------------
    # Construction du dict mergeable à partir de plugin_data et mod_data
    # ------------------------------------------------------------------

    def build_from_data(self, mod_data: dict) -> None:
        """
        Remplit self.mergeable en analysant plugin_data et mod_data.
        Équivalent du handler LOADCACHE dans mergeable.pl.

        Corrige le bug original : le flag DLL provenant de SKSE/F4SE/NVSE
        était affecté à une variable non définie (plugin_data[p]) ;
        il est maintenant correctement stocké dans self.mergeable.
        Support multi-jeux : SKSE (Skyrim), F4SE (Fallout 4), NVSE (Fallout NV)
        sont tous traités comme un flag 'DLL' bloquant.
        """
        self.mergeable = {}

        # ── Passe 1 : issues issues de plugin_data ──────────────────────────
        for plugin_name, pdata in self.plugin_data.items():
            if not pdata.get('Exists'):
                continue
            issues: dict[str, bool] = {}
            if pdata.get('ESM'):       issues['ESM']       = True
            if pdata.get('ESL'):       issues['ESL']       = True
            if pdata.get('Localized'): issues['Localized'] = True
            if pdata.get('NAVI'):      issues['NAVI']      = True
            if pdata.get('BaseGame'):  issues['BaseGame']  = True
            # Script-extenders (tous les jeux) → bloquent via flag DLL
            if pdata.get('SKSE') or pdata.get('F4SE') or pdata.get('NVSE'):
                issues['DLL'] = True
            self.mergeable[plugin_name] = issues

        # ── Passe 2 : issues issues de mod_data ─────────────────────────────
        for mod_name, mdata in mod_data.items():
            for plugin_raw in mdata.get('Plugins', {}):
                plugin_name = plugin_raw
                # Normaliser la casse
                for p in self.loadorder:
                    if p.lower() == plugin_raw.lower():
                        plugin_name = p
                        break
                if not self.plugin_data.get(plugin_name, {}).get('Exists'):
                    continue
                self.mergeable.setdefault(plugin_name, {})
                if mdata.get('MCM Quest') == 1:
                    self.mergeable[plugin_name]['MCM Quest'] = True
                if mdata.get('Archive'):
                    self.mergeable[plugin_name]['Archive'] = True
                    self.mergeable[plugin_name]['MCM Quest'] = True
                # Correction du bug : était `plugin_data[p]['DLL'] = True`
                # (variable non définie dans ce scope).
                # Tous les script-extenders → flag DLL bloquant.
                if mdata.get('SKSE') or mdata.get('F4SE') or mdata.get('NVSE'):
                    self.mergeable[plugin_name]['DLL'] = True

            for issue, affected_plugins in mdata.items():
                if issue in ('SKSE', 'F4SE', 'NVSE', 'MCM Quest', 'Installed', 'Plugins'):
                    continue
                if not isinstance(affected_plugins, dict):
                    continue
                for plugin_name in affected_plugins:
                    if self.plugin_data.get(plugin_name, {}).get('Exists'):
                        self.mergeable.setdefault(plugin_name, {})[issue] = True

    # ------------------------------------------------------------------
    # Activation des ignorances selon les DLL de support détectées
    # ------------------------------------------------------------------

    def enable_merge_mapper(self) -> None:
        """
        Quand MergeMapper.dll (Skyrim SE) est détecté, les issues supportées
        deviennent automatiquement ignorables.
        Équivalent du handler MERGEMAPPER dans mergeable.pl.
        """
        for issue in ('Script', 'DAR', 'OAR', 'SPID', 'KID', 'BOS', 'FML', 'AOS', 'IPM'):
            self.ignored_issues[issue] = True

    # ------------------------------------------------------------------
    # is_mergeable
    # ------------------------------------------------------------------

    def is_mergeable(self, plugin_name: str) -> int:
        """
        Retourne :
          0  → non mergeable
          1  → mergeable
         -1  → mergeable, mais a des enfants eux-mêmes mergeables (parent de merge)
          2  → mergeable mais porte NAVI (une seule par groupe autorisée)
        """
        if self.manual_decision.get(plugin_name) is False:
            return 0

        issues = self.mergeable.get(plugin_name, {})
        m = 1
        for criteria, present in issues.items():
            if criteria == 'NAVI':
                continue
            if not present:
                continue
            if not self.ignored_issues.get(criteria):
                m = 0
                break

        if m and issues.get('NAVI') and not self.ignored_issues.get('NAVI'):
            m = 2

        # Décision manuelle "force merge"
        if self.manual_decision.get(plugin_name):
            m = 1

        if m == 0:
            return 0

        # Vérifier les enfants
        children = self.plugin_data.get(plugin_name, {}).get('Children', [])
        if children:
            all_children_mergeable = all(
                abs(self.is_mergeable(c)) == 1 for c in children
            )
            if all_children_mergeable:
                m = m * -1   # Parent mergeable
            else:
                m = 0        # Enfant non mergeable → parent non mergeable

        return m

    # ------------------------------------------------------------------
    # Toggle mergeabilité manuelle
    # ------------------------------------------------------------------

    def toggle_mergeability(self, plugin_name: str) -> None:
        """Bascule la décision manuelle pour un plugin."""
        if self.manual_decision.get(plugin_name) is not None:
            self.manual_decision[plugin_name] = not self.manual_decision[plugin_name]
        else:
            current = abs(self.is_mergeable(plugin_name)) == 1
            self.manual_decision[plugin_name] = not current

    def ignore_flags(self, plugin_name: str) -> None:
        """Ignore toutes les issues du plugin sélectionné."""
        for issue in self.mergeable.get(plugin_name, {}):
            self.ignored_issues[issue] = True

    # ------------------------------------------------------------------
    # Calcul des groupes de merge (accurate)
    # ------------------------------------------------------------------

    def accurate_merge_groups(self,
                               lo: Optional[list[str]] = None) -> list[int]:
        """
        Calcule les groupes de merge en tenant compte de la limite de 250
        plugins+masters par groupe.
        Retourne une liste de même longueur que loadorder :
          0 → non mergeable (plugin actif seul)
          N → numéro du groupe de merge (1, 2, 3…)
        """
        if lo is None:
            lo = self.loadorder
        n = len(lo)
        results              = [0] * n
        merge_count          = 0
        in_merge: list[str]  = []
        in_merge_masters: dict     = {}
        unmergeable_masters: dict  = {}
        can_take_navi        = True

        for z in range(n - 1, -1, -1):
            mod = lo[z]
            m   = self.is_mergeable(mod)

            if m < 0:
                if mod in unmergeable_masters:
                    m = 0
                else:
                    m = -m

            if m == 2:
                if can_take_navi:
                    m = 1
                    can_take_navi = False
                else:
                    m = 0

            if m == 0:
                if len(in_merge) == 1:
                    results[z + 1] = 0
                    merge_count -= 1
                unmergeable_masters.update(in_merge_masters)
                self._add_plugin_masters(mod, unmergeable_masters)
                in_merge_masters = {}
                in_merge = []
                can_take_navi = True

            if m == 1:
                if not in_merge:
                    merge_count += 1
                self._add_plugin_masters(mod, in_merge_masters)
                if len(in_merge_masters) > self.MERGE_GROUP_LIMIT:
                    in_merge = []
                    in_merge_masters = {}
                    self._add_plugin_masters(mod, in_merge_masters)
                    merge_count += 1
                    can_take_navi = True
                in_merge.append(mod)

            results[z] = merge_count if m else 0

        if merge_count > 0:
            for a in range(n):
                if results[a] != 0:
                    results[a] = merge_count + 1 - results[a]

        return results

    def faster_merge_groups(self,
                             lo: Optional[list[str]] = None) -> list[int]:
        """
        Version rapide (~8× plus rapide) mais moins précise.
        Limite arbitraire à 150 plugins par groupe.
        Utilisée par AutoSorter pour évaluer les configurations rapidement.
        """
        if lo is None:
            lo = self.loadorder
        n = len(lo)
        results       = [0] * n
        merge_count   = 0
        in_merge: dict = {}
        can_take_navi  = True

        for z in range(n - 1, -1, -1):
            mod = lo[z]
            m   = self.is_mergeable(mod)

            if m < 0:
                children = self.plugin_data.get(mod, {}).get('Children', [])
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _add_plugin_masters(self, plugin: str, dest: dict) -> None:
        """Ajoute récursivement le plugin et tous ses masters dans dest."""
        for m in self.plugin_data.get(plugin, {}).get('Masters', []):
            if m not in dest:
                self._add_plugin_masters(m, dest)
        dest[plugin] = True

    def build_gui_list(self, selection: int = 0) -> list[list]:
        """
        Construit la liste formatée pour la GUI :
        [[merge_group, mergeable_value, display_name], ...]
        """
        mg = self.accurate_merge_groups()
        result = []
        for a, plugin_name in enumerate(self.loadorder):
            mg_num  = mg[a]
            m_val   = self.is_mergeable(plugin_name)
            issues  = self.mergeable.get(plugin_name, {})
            display = plugin_name
            for issue in sorted(issues):
                if not self.ignored_issues.get(issue):
                    display += f' [{issue}]'
            result.append([mg_num, m_val, display])
        return result

    def count_active(self, lo: Optional[list[str]] = None) -> float:
        """
        Compte le nombre de plugins "actifs" (non mergés) + pénalité de merge.
        Utilisé par AutoSorter pour évaluer une configuration de loadorder.
        """
        if lo is None:
            lo = self.loadorder
        mg = self.faster_merge_groups(lo)
        active             = 0
        current_merge_size = 0
        current_merge      = 0

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
