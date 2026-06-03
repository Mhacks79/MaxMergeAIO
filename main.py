"""
main.py
Point d'entrée de Koro — version Python.
Lance le serveur web et ouvre le navigateur.
"""

import sys
import os
import logging
import multiprocessing

# S'assurer qu'on cherche les modules dans le même dossier
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui_server import run

if __name__ == '__main__':
    # ÉTAPE 2 : INDISPENSABLE POUR PYINSTALLER AVEC MULTIPROCESSING
    multiprocessing.freeze_support()
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s'
    )
    run(host='127.0.0.1', port=5000, open_browser=True)
