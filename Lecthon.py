import os
import io
import json
import math
import logging
import platform
import queue
import random
import re
import subprocess
import threading
import time
import difflib
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, simpledialog
from audio_engine import create_audio_engine
from tinytag import TinyTag
from PIL import Image, ImageTk, ImageDraw
import requests
import mutagen
import mutagen.mp3
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC, ID3NoHeaderError
import musicbrainzngs

# Logger applicatif : les erreurs "attendues" (fichier corrompu, tag manquant, réseau
# indisponible...) sont avalées proprement dans l'UI pour ne pas interrompre l'utilisateur,
# mais tracées ici pour rester diagnosticables sans avoir à retirer les try/except.
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("lecteur_pro")


# --- Onglet YouTube (optionnel) : le module youtube_manager.py doit être placé à côté de
# ce fichier. Import protégé pour que l'application démarre quand même (avec l'onglet
# désactivé) si ytmusicapi/yt-dlp ne sont pas installés ou si le fichier est absent.
try:
    from youtube_manager import YouTubeManager
    YOUTUBE_TAB_AVAILABLE = True
except ImportError:
    YOUTUBE_TAB_AVAILABLE = False

PLAYLISTS_FILE = "playlists.json"
LIBRARY_CACHE_FILE = "library_cache.json"

# Sépare un champ "artiste" brut en artistes individuels, quels que soient la casse et le séparateur
# utilisés dans les tags (virgule, &, feat./feat, ft./ft, featuring, avec, with, x, vs...).
_ARTIST_SEPARATOR_RE = re.compile(
    r'\s*(?:,|;|/|&|\+|\bx\b|\bvs\.?(?!\w)|\bfeat\.?(?!\w)|\bft\.?(?!\w)|\bfeaturing\b|\bavec\b|\bwith\b)\s*',
    re.IGNORECASE
)

def parse_artist_names(raw_artist):
    """Découpe un champ artiste brut (ex: 'Alpha wann Feat. huntrill') en une liste de noms
    individuels ('Alpha wann', 'huntrill'), pour que chaque artiste soit reconnu indépendamment
    de la ponctuation, la casse ou le séparateur utilisé dans le tag d'origine."""
    if not raw_artist or not raw_artist.strip():
        return ["Inconnu"]
    parts = [p.strip() for p in _ARTIST_SEPARATOR_RE.split(raw_artist) if p.strip()]
    return parts if parts else ["Inconnu"]


# =========================================================
# ÉDITEUR DE MÉTADONNÉES — fonctions utilitaires (module-level)
# Portées telles quelles depuis "Editeur méta musique.py" (logique inchangée,
# seule l'interface a été adaptée/restylée pour l'intégration au Lecteur).
# =========================================================

musicbrainzngs.set_useragent("DurationTagger", "2.0", "example@mail.com")

FORMATS_SUPPORTES_EDITEUR = ('.mp3', '.webm')


def ffmpeg_disponible():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def detecter_faux_mp3(chemin):
    cl = chemin.lower()
    if cl.endswith('.webm'):
        return True
    if not cl.endswith('.mp3'):
        return False
    try:
        audio = mutagen.File(chemin)
        return audio is not None and not isinstance(audio, mutagen.mp3.MP3)
    except Exception:
        return False


ENCODEURS_MP3 = ["libmp3lame", "mp3", "libshine"]


def _encodeurs_disponibles():
    try:
        r = subprocess.run(["ffmpeg", "-encoders"], capture_output=True, timeout=10)
        sortie = r.stdout.decode(errors="replace") + r.stderr.decode(errors="replace")
        return [enc for enc in ENCODEURS_MP3 if enc in sortie]
    except Exception:
        return []


def convertir_en_mp3(chemin, callback_progression=None):
    cl = chemin.lower()
    chemin_sortie = chemin[:-5] + ".mp3" if cl.endswith('.webm') else chemin
    tmp = chemin_sortie + ".__tmp__.mp3"

    encodeurs = _encodeurs_disponibles()
    if not encodeurs:
        return None, "Aucun encodeur MP3 trouvé dans votre build ffmpeg."

    for encodeur in encodeurs:
        try:
            process = subprocess.Popen(
                ["ffmpeg", "-y", "-i", chemin, "-vn", "-codec:a", encodeur, "-q:a", "2", tmp],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                universal_newlines=True
            )

            while process.poll() is None:
                if callback_progression:
                    callback_progression()
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass

            if process.returncode == 0:
                if os.path.exists(chemin) and os.path.abspath(chemin) != os.path.abspath(chemin_sortie):
                    os.remove(chemin)
                if os.path.exists(tmp):
                    if os.path.exists(chemin_sortie) and os.path.abspath(tmp) != os.path.abspath(chemin_sortie):
                        os.remove(chemin_sortie)
                    os.rename(tmp, chemin_sortie)
                return chemin_sortie, ""
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            return None, str(e)

    return None, "Échec de conversion avec les encodeurs disponibles."


def clean_filename(filename):
    name = os.path.splitext(filename)[0]
    name = re.sub(r'\[.*?\]', '', name)
    name = re.sub(r'\(.*?\)', '', name)
    name = name.replace('-', ' ')
    return name.strip().lower()


def clean_youtube_name(text):
    text = text.lower()
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\(.*?\)', '', text)
    trash_words = [
        r'\bofficial\b', r'\bvideo\b', r'\baudio\b', r'\bclip\b', r'\bofficiel\b',
        r'\blyrics\b', r'\blyric\b', r'\b4k\b', r'\bhd\b', r'\b1080p\b', r'\bft\b', r'\bfeat\b'
    ]
    for word in trash_words:
        text = re.sub(word, '', text)

    text = re.sub(r'\s+', ' ', text)
    return text.strip().title()


def find_best_match(filename, file_duration, tracks_db, used_tracks):
    best_track = None
    best_score = -1.0
    threshold = 0.40

    clean_name = clean_filename(filename)
    raw_name = os.path.splitext(filename)[0].lower()

    for track_key, track_data in tracks_db.items():
        if track_key in used_tracks:
            continue

        db_title = track_data["title"].lower()
        db_duration = track_data["duration"]

        if db_title == clean_name or db_title == raw_name:
            score_text = 1.0
        else:
            score_clean = difflib.SequenceMatcher(None, clean_name, db_title).ratio()
            score_raw = difflib.SequenceMatcher(None, raw_name, db_title).ratio()
            score_text = max(score_clean, score_raw)

        if file_duration > 0 and db_duration > 0:
            diff_sec = abs(file_duration - db_duration)
            if diff_sec <= 2:
                score_duration = 1.0
            elif diff_sec <= 15:
                score_duration = 1.0 - ((diff_sec - 2) / 13.0)
            else:
                score_duration = 0.0
        else:
            score_duration = 0.5

        total_score = (score_text * 0.65) + (score_duration * 0.35)

        if total_score > best_score:
            best_score = total_score
            best_track = track_key

    if best_score >= threshold:
        return best_track, best_score
    return None, best_score


def find_release_by_track_count(artist, album, local_file_count):
    try:
        query = f'artist:"{artist}" AND release:"{album}"'
        result = musicbrainzngs.search_releases(query=query, limit=25)
        releases = result.get("release-list", [])
        if not releases:
            result = musicbrainzngs.search_releases(artist=artist, release=album, limit=25)
            releases = result.get("release-list", [])

        if not releases:
            return None

        for release in releases:
            mb_track_count = int(release.get("medium-track-count", 0))
            if mb_track_count == local_file_count:
                return release
    except Exception:
        pass
    return None


class MusicPlayer:
    def __init__(self, root):
        self.root = root
        self.root.title("Lecteur de Musique Pro")
        self.root.state('zoomed')
        self.root.configure(bg="#f8fafc")

        # Police système moderne (au lieu d'Arial, daté) : Segoe UI sous Windows, Helvetica
        # Neue sous macOS, DejaVu Sans sous Linux (toujours présente, contrairement à Arial).
        self.font_family = {"Windows": "Segoe UI", "Darwin": "Helvetica Neue"}.get(platform.system(), "DejaVu Sans")

        # Moteur audio : VLC si disponible (lecture progressive, seek fiable, fin de piste par
        # événement natif), repli automatique et transparent sur pygame sinon -- voir audio_engine.py.
        self.audio = create_audio_engine(self.root, prefer_vlc=True)
        self.audio.set_volume(0.7)
        self.audio.set_end_callback(self.play_next)  # remplace l'ancien polling check_music_end()

        # Variables d'état
        self.all_tracks = self.load_library_cache()
        self.tracks_by_path = {t["path"]: t for t in self.all_tracks}
        self.current_tracks = []     
        self.playlists = self.load_playlists()
        self.clean_playlists()
        self.current_view = "library" 
        
        self.current_album_name = None   # Nom de l'album actuellement affiché en détail
        self.current_album_tracks = []   # Morceaux de l'album affiché en détail
        self.current_artist_name = None    # Nom de l'artiste actuellement affiché en détail
        self.current_artist_tracks = []    # Morceaux de l'artiste affiché en détail
        
        self.albums_data = {}
        self.album_covers_cache = {}
        self.artists_data = {}

        # --- Grille d'albums ---
        self._selected_albums = set()      # Noms d'albums sélectionnés (Ctrl+clic pour multi-sélection)
        self._album_cards = {}             # nom_album -> widget carte, pour rafraîchir la sélection visuelle
        self._albums_dict_shown = {}       # Dernier dict affiché (pour retrier/reconstruire au redimensionnement)
        self._album_grid_cols = 4          # Nombre de colonnes courant (recalculé selon la largeur disponible)
        self._ALBUM_CARD_W = 150
        self._ALBUM_CARD_PAD = 14
        
        # --- NOUVEAU SYSTÈME DE FILE D'ATTENTE ---
        self.queue_manual = []       # Ajouts manuels (À lire ensuite)
        self.queue_auto = []         # Suite logique (Album/Playlist en cours)
        self.history = []            # Historique pour le bouton Précédent
        self.shuffle_mode = False    # Lecture aléatoire activée/désactivée
        self.repeat_mode = False     # Répétition du morceau en cours activée/désactivée
        
        self.track_length = 0        
        self.is_sliding = False      
        self.current_track_path = None 
        
        self.paused = False
        self.is_stopped = True       
        self.current_cover = None    

        # --- ARCHITECTURE DE L'INTERFACE ---
        # Icônes rondes dessinées (voir _build_playback_icons) : rendu net et cohérent, utilisées
        # par la sidebar ET la barre de contrôle -> générées avant tout le reste de l'UI.
        self._build_playback_icons()

        self.sidebar = tk.Frame(self.root, bg="#0f172a", width=240)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y)
        self.sidebar.pack_propagate(False)
        self.setup_sidebar()

        self.main_container = tk.Frame(self.root, bg="#f8fafc")
        self.main_container.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Conteneur de l'app "Lecteur" (masqué/affiché en entier selon le mode actif, voir §mode switcher)
        self.lecteur_app_frame = tk.Frame(self.main_container, bg="#f8fafc")
        self.lecteur_app_frame.pack(fill=tk.BOTH, expand=True)

        top_frame = tk.Frame(self.lecteur_app_frame, bg="#f8fafc")
        top_frame.pack(pady=15, fill=tk.X, padx=20)

        self.btn_select = tk.Button(
            top_frame, text="📁 Importer un dossier", font=(self.font_family, 10, "bold"),
            bg="#3b82f6", fg="white", relief=tk.FLAT, cursor="hand2", command=self.select_folder
        )
        self.btn_select.pack(side=tk.LEFT, padx=10)

        self.btn_clear_library = tk.Button(
            top_frame, text="🗑 Vider la bibliothèque", font=(self.font_family, 10, "bold"),
            bg="#ef4444", fg="white", relief=tk.FLAT, cursor="hand2", command=self.clear_library
        )
        self.btn_clear_library.pack(side=tk.LEFT, padx=10)

        search_frame = tk.Frame(top_frame, bg="#f8fafc")
        search_frame.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=20)
        
        tk.Label(search_frame, text="🔍", bg="#f8fafc", font=(self.font_family, 12)).pack(side=tk.LEFT)

        self.search_scope_var = tk.StringVar(value="Tous")  # Champ ciblé par la recherche avancée

        self.search_var = tk.StringVar()
        self.search_var.trace("w", self.filter_music)
        self.search_entry = tk.Entry(
            search_frame, textvariable=self.search_var, font=(self.font_family, 11), bg="white",
            relief=tk.FLAT, bd=0, highlightthickness=2, highlightbackground="#e2e8f0", highlightcolor="#3b82f6"
        )
        self.search_entry.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)

        # --- Recherche avancée : restreint la recherche à un seul champ (masqué par défaut) ---
        self.search_scope_combo = ttk.Combobox(
            search_frame, textvariable=self.search_scope_var, state="readonly", width=10,
            values=["Tous", "Titre", "Artiste", "Album", "Année"]
        )
        self.search_scope_combo.bind("<<ComboboxSelected>>", self.filter_music)

        self.btn_advanced_search = tk.Button(
            search_frame, text="⚙ Avancée", font=(self.font_family, 9, "bold"), bg="#64748b", fg="white",
            relief=tk.FLAT, cursor="hand2", command=self.toggle_advanced_search
        )
        self.btn_advanced_search.pack(side=tk.LEFT, padx=(0, 0))

        content_frame = tk.Frame(self.lecteur_app_frame, bg="#f8fafc")
        content_frame.pack(fill=tk.BOTH, expand=True, padx=20)

        self.list_frame = tk.Frame(content_frame)
        self.list_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview.Heading", font=(self.font_family, 10, "bold"), background="#e2e8f0", relief=tk.FLAT)
        style.configure("Treeview", font=(self.font_family, 10), rowheight=28, fieldbackground="white", background="white")
        # Ascenseur : on garde la mise en page standard du thème (fiable sur toutes les
        # plateformes) et on se contente de la recolorer -- une mise en page personnalisée
        # (sans flèches) s'est révélée ne pas afficher la poignée sur certains systèmes.
        style.configure("Modern.Vertical.TScrollbar", width=14, troughcolor="#e2e8f0",
                         background="#94a3b8", bordercolor="#e2e8f0", arrowcolor="#475569", relief=tk.FLAT)
        style.map("Modern.Vertical.TScrollbar", background=[("active", "#64748b"), ("pressed", "#475569")])

        # --- 1. CONTENEUR DES MUSIQUES ---
        self.track_view_frame = tk.Frame(self.list_frame)
        self.tree = ttk.Treeview(self.track_view_frame, columns=("titre", "artiste", "album", "genre", "annee"), show="headings", selectmode="extended")
        for col, text in zip(("titre", "artiste", "album", "genre", "annee"), ("Titre ↕", "Artiste ↕", "Album ↕", "Genre ↕", "Année ↕")):
            self.tree.heading(col, text=text, command=lambda c=col: self.sort_column(self.tree, c, False))
        self.tree.column("titre", width=220); self.tree.column("artiste", width=160); self.tree.column("album", width=160); self.tree.column("genre", width=110); self.tree.column("annee", width=80, anchor="center")
        tree_scrollbar = ttk.Scrollbar(self.track_view_frame, orient=tk.VERTICAL, command=self.tree.yview, style="Modern.Vertical.TScrollbar")
        tree_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=tree_scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # --- 2. CONTENEUR DES ALBUMS (grille de pochettes) ---
        self.album_view_frame = tk.Frame(self.list_frame, bg="#f8fafc")

        albums_toolbar = tk.Frame(self.album_view_frame, bg="#f8fafc")
        albums_toolbar.pack(fill=tk.X, pady=(0, 10))
        tk.Label(albums_toolbar, text="Trier par :", font=(self.font_family, 9), bg="#f8fafc", fg="#64748b").pack(side=tk.LEFT, padx=(0, 8))
        self.album_sort_var = tk.StringVar(value="Nom")
        album_sort_combo = ttk.Combobox(albums_toolbar, textvariable=self.album_sort_var, state="readonly", width=10, values=["Nom", "Année", "Genre"])
        album_sort_combo.pack(side=tk.LEFT)
        album_sort_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_album_grid(self._albums_dict_shown))

        albums_canvas_area = tk.Frame(self.album_view_frame)
        albums_canvas_area.pack(fill=tk.BOTH, expand=True)
        self.album_canvas = tk.Canvas(albums_canvas_area, bg="#f8fafc", highlightthickness=0)
        album_scrollbar = ttk.Scrollbar(albums_canvas_area, orient=tk.VERTICAL, command=self.album_canvas.yview, style="Modern.Vertical.TScrollbar")
        self.album_canvas.configure(yscrollcommand=album_scrollbar.set)
        album_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.album_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.album_grid_frame = tk.Frame(self.album_canvas, bg="#f8fafc")
        self._album_grid_window = self.album_canvas.create_window((0, 0), window=self.album_grid_frame, anchor="nw")
        self.album_grid_frame.bind("<Configure>", lambda e: self.album_canvas.configure(scrollregion=self.album_canvas.bbox("all")))
        self.album_canvas.bind("<Configure>", self._on_album_canvas_resize)
        self.album_canvas.bind("<MouseWheel>", self._on_album_mousewheel)
        self.album_canvas.bind("<Button-4>", lambda e: self.album_canvas.yview_scroll(-3, "units"))  # molette Linux (haut)
        self.album_canvas.bind("<Button-5>", lambda e: self.album_canvas.yview_scroll(3, "units"))   # molette Linux (bas)

        # --- 3. CONTENEUR DE LA FILE D'ATTENTE ---
        self.queue_view_frame = tk.Frame(self.list_frame)
        self.queue_tree = ttk.Treeview(self.queue_view_frame, columns=("etat", "titre", "album", "annee"), show="headings", selectmode="browse")
        self.queue_tree.heading("etat", text="Origine"); self.queue_tree.heading("titre", text="Titre"); self.queue_tree.heading("album", text="Album"); self.queue_tree.heading("annee", text="Année")
        self.queue_tree.column("etat", width=120, anchor="center"); self.queue_tree.column("titre", width=250); self.queue_tree.column("album", width=180); self.queue_tree.column("annee", width=80, anchor="center")
        queue_scrollbar = ttk.Scrollbar(self.queue_view_frame, orient=tk.VERTICAL, command=self.queue_tree.yview, style="Modern.Vertical.TScrollbar")
        queue_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.queue_tree.configure(yscrollcommand=queue_scrollbar.set)
        self.queue_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # --- 4. CONTENEUR DU DÉTAIL D'ALBUM (liste des morceaux d'un album pour sélection précise) ---
        self.album_detail_frame = tk.Frame(self.list_frame)

        detail_header = tk.Frame(self.album_detail_frame, bg="#f8fafc")
        detail_header.pack(fill=tk.X, pady=(0, 10))
        tk.Button(
            detail_header, text="⬅ Retour aux albums", font=(self.font_family, 9, "bold"),
            bg="#64748b", fg="white", relief=tk.FLAT, cursor="hand2", command=self.show_albums
        ).pack(side=tk.LEFT)
        self.lbl_album_detail_title = tk.Label(detail_header, text="", font=(self.font_family, 11, "bold"), bg="#f8fafc", fg="#1e293b")
        self.lbl_album_detail_title.pack(side=tk.LEFT, padx=15)

        detail_body = tk.Frame(self.album_detail_frame)
        detail_body.pack(fill=tk.BOTH, expand=True)
        self.album_detail_tree = ttk.Treeview(detail_body, columns=("titre", "artiste", "genre", "annee"), show="headings", selectmode="extended")
        for col, text in zip(("titre", "artiste", "genre", "annee"), ("Titre ↕", "Artiste ↕", "Genre ↕", "Année ↕")):
            self.album_detail_tree.heading(col, text=text, command=lambda c=col: self.sort_column(self.album_detail_tree, c, False))
        self.album_detail_tree.column("titre", width=260); self.album_detail_tree.column("artiste", width=160); self.album_detail_tree.column("genre", width=120); self.album_detail_tree.column("annee", width=80, anchor="center")
        album_detail_scrollbar = ttk.Scrollbar(detail_body, orient=tk.VERTICAL, command=self.album_detail_tree.yview, style="Modern.Vertical.TScrollbar")
        album_detail_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.album_detail_tree.configure(yscrollcommand=album_detail_scrollbar.set)
        self.album_detail_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # --- 5. CONTENEUR DES ARTISTES ---
        self.artist_view_frame = tk.Frame(self.list_frame)
        self.artist_tree = ttk.Treeview(self.artist_view_frame, columns=("artiste", "nb_titres", "genre"), show="headings", selectmode="extended")
        for col, text in zip(("artiste", "nb_titres", "genre"), ("Artiste ↕", "Titres ↕", "Genre principal ↕")):
            self.artist_tree.heading(col, text=text, command=lambda c=col: self.sort_column(self.artist_tree, c, False))
        self.artist_tree.column("artiste", width=280); self.artist_tree.column("nb_titres", width=90, anchor="center"); self.artist_tree.column("genre", width=150)
        artist_scrollbar = ttk.Scrollbar(self.artist_view_frame, orient=tk.VERTICAL, command=self.artist_tree.yview, style="Modern.Vertical.TScrollbar")
        artist_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.artist_tree.configure(yscrollcommand=artist_scrollbar.set)
        self.artist_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # --- 6. CONTENEUR DU DÉTAIL D'ARTISTE (morceaux d'un artiste, triables par année de sortie) ---
        self.artist_detail_frame = tk.Frame(self.list_frame)

        artist_detail_header = tk.Frame(self.artist_detail_frame, bg="#f8fafc")
        artist_detail_header.pack(fill=tk.X, pady=(0, 10))
        tk.Button(
            artist_detail_header, text="⬅ Retour aux artistes", font=(self.font_family, 9, "bold"),
            bg="#64748b", fg="white", relief=tk.FLAT, cursor="hand2", command=self.show_artists
        ).pack(side=tk.LEFT)
        self.lbl_artist_detail_title = tk.Label(artist_detail_header, text="", font=(self.font_family, 11, "bold"), bg="#f8fafc", fg="#1e293b")
        self.lbl_artist_detail_title.pack(side=tk.LEFT, padx=15)

        artist_detail_body = tk.Frame(self.artist_detail_frame)
        artist_detail_body.pack(fill=tk.BOTH, expand=True)
        self.artist_detail_tree = ttk.Treeview(artist_detail_body, columns=("titre", "album", "genre", "annee"), show="headings", selectmode="extended")
        for col, text in zip(("titre", "album", "genre", "annee"), ("Titre ↕", "Album ↕", "Genre ↕", "Année ↕")):
            self.artist_detail_tree.heading(col, text=text, command=lambda c=col: self.sort_column(self.artist_detail_tree, c, False))
        self.artist_detail_tree.column("titre", width=260); self.artist_detail_tree.column("album", width=180); self.artist_detail_tree.column("genre", width=120); self.artist_detail_tree.column("annee", width=80, anchor="center")
        artist_detail_scrollbar = ttk.Scrollbar(artist_detail_body, orient=tk.VERTICAL, command=self.artist_detail_tree.yview, style="Modern.Vertical.TScrollbar")
        artist_detail_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.artist_detail_tree.configure(yscrollcommand=artist_detail_scrollbar.set)
        self.artist_detail_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Affichage par défaut
        self.track_view_frame.pack(fill=tk.BOTH, expand=True)
        
        # Événements Musiques & Albums
        self.tree.bind("<Double-1>", lambda e: self.on_double_click_track())
        self.tree.bind("<Button-3>", self.show_context_menu)
        self.tree.bind("<B1-Motion>", lambda e: self.on_drag_select(e, self.tree))

        # (Vue Albums : les événements sont bindés directement sur chaque carte, voir _build_album_card)
        
        # Événements File d'attente (Double clic et Glisser-Déposer)
        self.queue_tree.bind("<Double-1>", self.on_double_click_queue)
        self.queue_tree.bind("<ButtonPress-1>", self.on_queue_press)
        self.queue_tree.bind("<ButtonRelease-1>", self.on_queue_release)

        # Événements Détail d'album (sélection des morceaux à écouter)
        self.album_detail_tree.bind("<Double-1>", lambda e: self.on_double_click_album_track())
        self.album_detail_tree.bind("<Button-3>", self.show_context_menu)
        self.album_detail_tree.bind("<B1-Motion>", lambda e: self.on_drag_select(e, self.album_detail_tree))

        # Événements Artistes & Détail d'artiste
        self.artist_tree.bind("<Double-1>", lambda e: self.on_double_click_artist())
        self.artist_tree.bind("<Button-3>", self.show_context_menu)
        self.artist_tree.bind("<B1-Motion>", lambda e: self.on_drag_select(e, self.artist_tree))

        self.artist_detail_tree.bind("<Double-1>", lambda e: self.on_double_click_artist_track())
        self.artist_detail_tree.bind("<Button-3>", self.show_context_menu)
        self.artist_detail_tree.bind("<B1-Motion>", lambda e: self.on_drag_select(e, self.artist_detail_tree))

        # --- PANNEAU LECTEUR (Droite) ---
        self.player_panel = tk.Frame(content_frame, bg="#f8fafc", width=280)
        self.player_panel.pack(side=tk.RIGHT, fill=tk.BOTH, padx=20)
        self.player_panel.pack_propagate(False)

        self.cover_label = tk.Label(self.player_panel, bg="#f8fafc")
        self.cover_label.pack(pady=20)
        self.set_placeholder_cover()

        self.lbl_now_playing_title = tk.Label(self.player_panel, text="Aucun morceau", font=(self.font_family, 11, "bold"), bg="#f8fafc", fg="#1e293b", wraplength=240, justify="center")
        self.lbl_now_playing_title.pack(pady=5)
        self.lbl_now_playing_album = tk.Label(self.player_panel, text="", font=(self.font_family, 9), bg="#f8fafc", fg="#64748b", wraplength=240, justify="center")
        self.lbl_now_playing_album.pack()

        # Conteneur de l'app "Editeur" (métadonnées) : construit tout de suite mais jamais
        # packé par défaut -> show_app_editeur()/show_app_lecteur() basculent entre les deux.
        self.editor_app_frame = tk.Frame(self.main_container, bg="#f8fafc")
        self.metadata_editor = MetadataEditor(self.editor_app_frame, self.root, self.font_family, self.icons)

        # Conteneur de l'app "YouTube" : streaming YouTube Music mis en cache (voir youtube_manager.py).
        # self.youtube_manager n'est instancié qu'à la connexion (depuis l'onglet), pas au démarrage,
        # pour ne jamais bloquer/faire planter le lancement de l'appli si l'oauth ou le réseau posent problème.
        self.youtube_manager = None
        self.youtube_source_active = False  # True si la piste en cours vient du tampon/de la sélection YouTube
        self.youtube_app_frame = tk.Frame(self.main_container, bg="#f8fafc")
        if YOUTUBE_TAB_AVAILABLE:
            self.youtube_tab = YoutubeTab(self.youtube_app_frame, self.root, self.font_family, self)
        else:
            tk.Label(
                self.youtube_app_frame,
                text="Onglet YouTube indisponible.\n\nInstalle ytmusicapi et yt-dlp, et place "
                     "youtube_manager.py à côté de ce fichier, puis relance l'application.",
                font=(self.font_family, 11), bg="#f8fafc", fg="#94a3b8", justify="center"
            ).pack(expand=True)

        # --- BARRE DE PROGRESSION ET CONTRÔLES (Bas) ---
        controls_container = tk.Frame(self.main_container, bg="#f1f5f9", height=140)
        controls_container.pack(side=tk.BOTTOM, fill=tk.X)
        controls_container.pack_propagate(False)

        progress_frame = tk.Frame(controls_container, bg="#f1f5f9")
        progress_frame.pack(fill=tk.X, padx=40, pady=(12, 4))

        self.lbl_time_current = tk.Label(progress_frame, text="00:00", bg="#f1f5f9", font=(self.font_family, 9, "bold"), fg="#475569")
        self.lbl_time_current.pack(side=tk.LEFT)

        self.build_progress_slider(progress_frame)

        self.lbl_time_total = tk.Label(progress_frame, text="00:00", bg="#f1f5f9", font=(self.font_family, 9, "bold"), fg="#475569")
        self.lbl_time_total.pack(side=tk.RIGHT)

        controls_inner = tk.Frame(controls_container, bg="#f1f5f9")
        controls_inner.pack(expand=True)

        def round_button(icon_key, command, padx):
            btn = tk.Button(
                controls_inner, image=self.icons[icon_key], bg="#f1f5f9", activebackground="#f1f5f9",
                relief=tk.FLAT, bd=0, highlightthickness=0, cursor="hand2", takefocus=0, command=command
            )
            btn.pack(side=tk.LEFT, padx=padx)
            return btn

        self.btn_shuffle = round_button("shuffle_off", self.toggle_shuffle, (0, 8))
        self.btn_shuffle.bind("<Enter>", lambda e: self.btn_shuffle.config(image=self.icons["shuffle_on_hover" if self.shuffle_mode else "shuffle_off_hover"]))
        self.btn_shuffle.bind("<Leave>", lambda e: self.btn_shuffle.config(image=self.icons["shuffle_on" if self.shuffle_mode else "shuffle_off"]))

        self.btn_prev = round_button("prev", self.play_prev, 10)
        self.btn_prev.bind("<Enter>", lambda e: self.btn_prev.config(image=self.icons["prev_hover"]))
        self.btn_prev.bind("<Leave>", lambda e: self.btn_prev.config(image=self.icons["prev"]))

        # --- BOUTON UNIQUE LECTURE/PAUSE : bascule automatiquement son icône/couleur selon l'état ---
        self.btn_play_pause = round_button("play", self.toggle_play_pause, 14)
        self.btn_play_pause.bind("<Enter>", lambda e: self.btn_play_pause.config(image=self.icons["pause_hover" if not (self.is_stopped or self.paused) else "play_hover"]))
        self.btn_play_pause.bind("<Leave>", lambda e: self.update_play_pause_button())

        self.btn_next = round_button("next", self.play_next, 10)
        self.btn_next.bind("<Enter>", lambda e: self.btn_next.config(image=self.icons["next_hover"]))
        self.btn_next.bind("<Leave>", lambda e: self.btn_next.config(image=self.icons["next"]))

        self.btn_stop = round_button("stop", self.stop, 10)
        self.btn_stop.bind("<Enter>", lambda e: self.btn_stop.config(image=self.icons["stop_hover"]))
        self.btn_stop.bind("<Leave>", lambda e: self.btn_stop.config(image=self.icons["stop"]))

        self.btn_repeat = round_button("repeat_off", self.toggle_repeat, (8, 20))
        self.btn_repeat.bind("<Enter>", lambda e: self.btn_repeat.config(image=self.icons["repeat_on_hover" if self.repeat_mode else "repeat_off_hover"]))
        self.btn_repeat.bind("<Leave>", lambda e: self.btn_repeat.config(image=self.icons["repeat_on" if self.repeat_mode else "repeat_off"]))

        # --- Curseur de volume (dessiné sur mesure : piste remplie + poignée, plus lisible qu'un ttk.Scale nu) ---
        volume_frame = tk.Frame(controls_inner, bg="#f1f5f9")
        volume_frame.pack(side=tk.LEFT)
        self.lbl_volume_icon = tk.Label(volume_frame, image=self.icons["volume"], bg="#f1f5f9", cursor="hand2")
        self.lbl_volume_icon.pack(side=tk.LEFT, padx=(0, 8))
        self.lbl_volume_icon.bind("<Button-1>", self.toggle_mute)

        self.volume_var = tk.DoubleVar(value=70)
        self._pre_mute_volume = 70  # volume à restaurer si on désactive le mode muet
        self.build_volume_slider(volume_frame, width=100, height=20)

        # --- Libellé indiquant clairement si Aléatoire ou Répétition est actif (vide sinon) ---
        self.lbl_playback_mode = tk.Label(controls_container, text="", font=(self.font_family, 8, "italic"), bg="#f1f5f9", fg="#3b82f6")
        self.lbl_playback_mode.pack(side=tk.BOTTOM, pady=(0, 4))

        self.update_timer()

        # Si une bibliothèque était déjà en cache, on l'affiche immédiatement (pas besoin de rescanner)
        if self.all_tracks:
            self.build_albums_data()
            self.build_artists_data()
            self.show_library()

        # Couper la musique proprement si l'utilisateur ferme la fenêtre
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # Raccourci clavier : Espace = Lecture/Pause (sauf si on écrit dans un champ de texte)
        self.root.bind_all("<space>", self.on_space_pressed)

    def on_space_pressed(self, event):
        """Bascule Lecture/Pause avec la barre Espace, sauf si le focus est dans un champ de saisie (recherche, dialogues...)."""
        if isinstance(event.widget, (tk.Entry, ttk.Entry)):
            return
        self.toggle_play_pause()
        return "break"

    def on_closing(self):
        """Arrête la lecture avant de fermer l'application (évite que la musique continue en arrière-plan)."""
        try:
            self.audio.shutdown()
        except Exception:
            pass
        if getattr(self, "youtube_manager", None):
            try:
                self.youtube_manager.stop()  # arrête le thread de chargement et nettoie le cache disque
            except Exception:
                pass
        self.root.quit()
        self.root.destroy()

    # --- SÉLECTION DE DOSSIER ET THREADING ---
    def select_folder(self):
        folder = filedialog.askdirectory()
        if not folder: return
        self.progress_win = tk.Toplevel(self.root)
        self.progress_win.title("Analyse...")
        self.progress_win.geometry("400x150")
        self.progress_win.transient(self.root)
        self.progress_win.grab_set()

        self.lbl_progress = tk.Label(self.progress_win, text="Recherche des fichiers audio...", font=(self.font_family, 10))
        self.lbl_progress.pack(pady=15)
        self.progress_bar = ttk.Progressbar(self.progress_win, orient=tk.HORIZONTAL, length=320, mode='determinate')
        self.progress_bar.pack(pady=10)

        self.scan_queue = queue.Queue()
        existing_paths = {t["path"] for t in self.all_tracks}
        threading.Thread(target=self.scan_folder_worker, args=(folder, existing_paths), daemon=True).start()
        self.listen_to_scan_queue()

    def scan_folder_worker(self, folder, existing_paths):
        supported_extensions = ('.mp3', '.wav', '.ogg', '.flac')
        all_filepaths = []
        for root_dir, _, files in os.walk(folder):
            for file in files:
                if file.lower().endswith(supported_extensions):
                    full_path = os.path.join(root_dir, file)
                    if full_path not in existing_paths:  # On ignore les morceaux déjà présents dans la bibliothèque
                        all_filepaths.append(full_path)

        total_files = len(all_filepaths)
        if total_files == 0:
            self.scan_queue.put(("done", []))
            return

        tracks = []
        for idx, full_path in enumerate(all_filepaths):
            try:
                tag = TinyTag.get(full_path)
                titre = tag.title if tag.title else os.path.basename(full_path)
                artiste = tag.artist if tag.artist else "Inconnu"
                album = tag.album if tag.album else "Inconnu"
                genre = tag.genre if tag.genre else "Inconnu"
                annee = str(int(tag.year)) if (tag.year and str(tag.year).isdigit()) else "Inconnu"
                duree = tag.duration if tag.duration else 0
            except Exception:
                titre, artiste, album, genre, annee, duree = os.path.basename(full_path), "Inconnu", "Inconnu", "Inconnu", "Inconnu", 0

            tracks.append({"path": full_path, "titre": titre, "artiste": artiste, "album": album, "genre": genre, "annee": annee, "duree": duree})
            if idx % 5 == 0 or idx == total_files - 1:
                self.scan_queue.put(("progress", idx + 1, total_files, titre))

        self.scan_queue.put(("done", tracks))

    def listen_to_scan_queue(self):
        try:
            while True:
                msg = self.scan_queue.get_nowait()
                if msg[0] == "progress":
                    self.progress_bar.config(maximum=msg[2], value=msg[1])
                    self.lbl_progress.config(text=f"Analyse : {msg[1]}/{msg[2]}\n{msg[3][:45]}...")
                elif msg[0] == "done":
                    added = len(msg[1])
                    self.all_tracks.extend(msg[1])
                    self.build_albums_data()
                    self.build_artists_data()
                    self.build_track_index()
                    self.show_library()
                    self.save_library_cache()
                    self.progress_win.destroy()
                    if added == 0:
                        messagebox.showinfo("Importer un dossier", "Aucun nouveau morceau trouvé (déjà présents dans la bibliothèque, ou aucun fichier audio dans ce dossier).")
                    return
        except queue.Empty:
            pass
        self.root.after(50, self.listen_to_scan_queue)

    # --- INTERFACE SIDEBAR ---
    def setup_sidebar(self):
        # --- Sélecteur d'application : Lecteur / Editeur (remplace l'ancien titre statique) ---
        mode_bar = tk.Frame(self.sidebar, bg="#0f172a")
        mode_bar.pack(fill=tk.X, padx=16, pady=(20, 16))
        self.btn_app_lecteur = tk.Button(
            mode_bar, text=" Lecteur", image=self.icons["app_note"], compound=tk.LEFT,
            font=(self.font_family, 11, "bold"), relief=tk.FLAT, bd=0, highlightthickness=0,
            cursor="hand2", takefocus=0, padx=8, pady=8, command=self.show_app_lecteur
        )
        self.btn_app_lecteur.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 3))
        self.btn_app_editeur = tk.Button(
            mode_bar, text=" Editeur", image=self.icons["app_edit"], compound=tk.LEFT,
            font=(self.font_family, 11, "bold"), relief=tk.FLAT, bd=0, highlightthickness=0,
            cursor="hand2", takefocus=0, padx=8, pady=8, command=self.show_app_editeur
        )
        self.btn_app_editeur.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=3)

        self.btn_app_youtube = tk.Button(
            mode_bar, text=" YouTube", compound=tk.LEFT,
            font=(self.font_family, 11, "bold"), relief=tk.FLAT, bd=0, highlightthickness=0,
            cursor="hand2", takefocus=0, padx=8, pady=8, command=self.show_app_youtube
        )
        self.btn_app_youtube.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(3, 0))
        tk.Frame(self.sidebar, bg="#1e293b", height=1).pack(fill=tk.X, padx=20)

        # --- Corps de la sidebar (nav + playlists) : masqué en entier en mode Editeur ---
        self.sidebar_body = tk.Frame(self.sidebar, bg="#0f172a")
        self.sidebar_body.pack(fill=tk.BOTH, expand=True)

        self._nav_meta = {}
        nav_frame = tk.Frame(self.sidebar_body, bg="#0f172a")
        nav_frame.pack(fill=tk.X, pady=(14, 6))

        self.btn_nav_queue = self._build_nav_item(nav_frame, "nav_queue", "File d'attente", self.show_queue, accent="#f59e0b")
        tk.Frame(nav_frame, bg="#0f172a", height=8).pack()  # petit espace avant le groupe principal
        self.btn_nav_library = self._build_nav_item(nav_frame, "nav_home", "Musiques", self.show_library)
        self.btn_nav_albums = self._build_nav_item(nav_frame, "nav_disc", "Albums", self.show_albums)
        self.btn_nav_artists = self._build_nav_item(nav_frame, "nav_mic", "Artistes", self.show_artists)

        self._nav_buttons = {
            "queue": self.btn_nav_queue, "library": self.btn_nav_library,
            "albums": self.btn_nav_albums, "album_detail": self.btn_nav_albums,
            "artists": self.btn_nav_artists, "artist_detail": self.btn_nav_artists,
        }

        self.lbl_sidebar_view = tk.Label(self.sidebar_body, text="🏠 Musiques", font=(self.font_family, 10, "italic"), bg="#0f172a", fg="#3b82f6")
        self.lbl_sidebar_view.pack(pady=10)

        tk.Frame(self.sidebar_body, bg="#1e293b", height=1).pack(fill=tk.X, padx=15, pady=(4, 0))
        tk.Label(self.sidebar_body, text="📋 MES PLAYLISTS", font=(self.font_family, 9, "bold"), bg="#0f172a", fg="#64748b", anchor="w", padx=15).pack(fill=tk.X, pady=(15, 5))

        pl_frame = tk.Frame(self.sidebar_body, bg="#1e293b")
        pl_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        pl_scrollbar = ttk.Scrollbar(pl_frame, orient=tk.VERTICAL, style="Modern.Vertical.TScrollbar")
        pl_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.pl_listbox = tk.Listbox(
            pl_frame, bg="#1e293b", fg="#f1f5f9", selectbackground="#3b82f6", selectforeground="white",
            font=(self.font_family, 10), bd=0, highlightthickness=0, activestyle="none",
            yscrollcommand=pl_scrollbar.set
        )
        self.pl_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        pl_scrollbar.config(command=self.pl_listbox.yview)
        self.pl_listbox.bind("<<ListboxSelect>>", self.on_playlist_select)

        btn_frame = tk.Frame(self.sidebar_body, bg="#0f172a")
        btn_frame.pack(pady=12)
        self.btn_pl_add = tk.Button(btn_frame, image=self.icons["pl_add"], bg="#0f172a", activebackground="#0f172a",
                                     relief=tk.FLAT, bd=0, highlightthickness=0, cursor="hand2", takefocus=0, command=self.create_playlist)
        self.btn_pl_add.pack(side=tk.LEFT, padx=12)
        self.btn_pl_add.bind("<Enter>", lambda e: self.btn_pl_add.config(image=self.icons["pl_add_hover"]))
        self.btn_pl_add.bind("<Leave>", lambda e: self.btn_pl_add.config(image=self.icons["pl_add"]))

        self.btn_pl_delete = tk.Button(btn_frame, image=self.icons["pl_delete"], bg="#0f172a", activebackground="#0f172a",
                                        relief=tk.FLAT, bd=0, highlightthickness=0, cursor="hand2", takefocus=0, command=self.delete_playlist)
        self.btn_pl_delete.pack(side=tk.LEFT, padx=12)
        self.btn_pl_delete.bind("<Enter>", lambda e: self.btn_pl_delete.config(image=self.icons["pl_delete_hover"]))
        self.btn_pl_delete.bind("<Leave>", lambda e: self.btn_pl_delete.config(image=self.icons["pl_delete"]))

        self.refresh_playlists_listbox()
        self.update_sidebar_highlight("library")
        self.update_app_mode_highlight("lecteur")


    def _build_nav_item(self, parent, icon_key, label, command, accent="#3b82f6"):
        """Construit une ligne de navigation moderne : fine barre d'accent à gauche (visible
        seulement si la section est active) + icône vectorielle + libellé. Remplace l'ancien
        bouton plein bloc de couleur par un style plus proche des sidebars actuelles
        (Discord/Notion/VSCode) : fond très légèrement teinté plutôt qu'un aplat saturé."""
        row = tk.Frame(parent, bg="#0f172a")
        row.pack(fill=tk.X, padx=(0, 10))
        bar = tk.Frame(row, bg="#0f172a", width=4)
        bar.pack(side=tk.LEFT, fill=tk.Y)
        btn = tk.Button(
            row, text=label, image=self.icons[icon_key], compound=tk.LEFT,
            font=(self.font_family, 11, "bold"), bg="#0f172a", fg="#94a3b8", activebackground="#0f172a",
            activeforeground="#f1f5f9", relief=tk.FLAT, bd=0, highlightthickness=0, cursor="hand2",
            anchor="w", padx=14, pady=11, takefocus=0, command=command
        )
        btn.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._nav_meta[btn] = {"bar": bar, "accent": accent}
        self._add_nav_hover(btn)
        return btn

    def _tint(self, accent_hex, amount=0.16):
        """Mélange une couleur d'accent avec le fond sombre de la sidebar, pour un fond d'état
        actif discret plutôt qu'un aplat saturé plein écran."""
        base = "1e293b"
        a = accent_hex.lstrip('#')
        r1, g1, b1 = (int(a[i:i + 2], 16) for i in (0, 2, 4))
        r2, g2, b2 = (int(base[i:i + 2], 16) for i in (0, 2, 4))
        r, g, b = (int(c2 + (c1 - c2) * amount) for c1, c2 in ((r1, r2), (g1, g2), (b1, b2)))
        return f"#{r:02x}{g:02x}{b:02x}"

    def _add_nav_hover(self, btn):
        """Éclaircit légèrement un item de nav au survol, sauf s'il est actuellement actif."""
        meta = self._nav_meta[btn]
        def on_enter(e):
            if meta["bar"]["bg"] == "#0f172a":  # pas actif
                btn.config(bg="#1e293b")
        def on_leave(e):
            if meta["bar"]["bg"] == "#0f172a":  # toujours pas actif
                btn.config(bg="#0f172a")
        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)

    def update_sidebar_highlight(self, view):
        """Met en avant l'item de navigation correspondant à la section actuellement affichée :
        barre d'accent colorée + fond légèrement teinté + texte plus clair. Aucun item mis en
        avant pour une playlist (déjà signalée par la sélection dans self.pl_listbox)."""
        active_btn = self._nav_buttons.get(view)
        for btn, meta in self._nav_meta.items():
            is_active = (btn is active_btn)
            meta["bar"].config(bg=meta["accent"] if is_active else "#0f172a")
            btn.config(bg=self._tint(meta["accent"]) if is_active else "#0f172a",
                       fg="#f1f5f9" if is_active else "#94a3b8")

    def update_app_mode_highlight(self, mode):
        """Met en surbrillance le bouton d'app actif (Lecteur/Editeur/YouTube) dans le sélecteur du haut."""
        active_bg, inactive_bg = "#3b82f6", "#1e293b"
        active_fg, inactive_fg = "#ffffff", "#94a3b8"
        for btn, btn_mode in ((self.btn_app_lecteur, "lecteur"),
                               (self.btn_app_editeur, "editeur"),
                               (self.btn_app_youtube, "youtube")):
            is_active = (mode == btn_mode)
            btn.config(bg=active_bg if is_active else inactive_bg,
                       fg=active_fg if is_active else inactive_fg,
                       activebackground=active_bg if is_active else inactive_bg)

    def show_app_lecteur(self):
        """Bascule vers l'app Lecteur : ré-affiche le contenu musical et la nav complète de la
        sidebar. La lecture en cours (moteur audio, timers) n'est jamais interrompue par ce switch —
        seule la vue change."""
        self.editor_app_frame.pack_forget()
        self.youtube_app_frame.pack_forget()
        self.lecteur_app_frame.pack(fill=tk.BOTH, expand=True)
        self.sidebar_body.pack(fill=tk.BOTH, expand=True)
        self.update_app_mode_highlight("lecteur")

    def show_app_editeur(self):
        """Bascule vers l'app Editeur (métadonnées) : masque tout le contenu Lecteur ainsi que
        la nav/playlists de la sidebar (non pertinentes ici). La musique continue de jouer en
        fond si elle était en cours : rien dans le moteur audio/les timers n'est touché par ce switch."""
        self.lecteur_app_frame.pack_forget()
        self.youtube_app_frame.pack_forget()
        self.sidebar_body.pack_forget()
        self.editor_app_frame.pack(fill=tk.BOTH, expand=True)
        self.update_app_mode_highlight("editeur")

    def show_app_youtube(self):
        """Bascule vers l'onglet YouTube : masque Lecteur/Editeur et la nav de la sidebar (non
        pertinente ici). La lecture en cours continue en fond, comme pour les autres modes."""
        self.lecteur_app_frame.pack_forget()
        self.editor_app_frame.pack_forget()
        self.sidebar_body.pack_forget()
        self.youtube_app_frame.pack(fill=tk.BOTH, expand=True)
        self.update_app_mode_highlight("youtube")


    def hide_all_views(self):
        self.track_view_frame.pack_forget()
        self.album_view_frame.pack_forget()
        self.queue_view_frame.pack_forget()
        self.album_detail_frame.pack_forget()
        self.artist_view_frame.pack_forget()
        self.artist_detail_frame.pack_forget()

    def show_library(self):
        self.current_view = "library"
        self.lbl_sidebar_view.config(text="🏠 Musiques")
        self.update_sidebar_highlight("library")
        self.pl_listbox.selection_clear(0, tk.END)
        self.hide_all_views()
        self.track_view_frame.pack(fill=tk.BOTH, expand=True)
        self.refresh_tree(self.all_tracks)

    def show_albums(self):
        self.current_view = "albums"
        self.lbl_sidebar_view.config(text="💿 Albums")
        self.update_sidebar_highlight("albums")
        self.pl_listbox.selection_clear(0, tk.END)
        self.hide_all_views()
        self.album_view_frame.pack(fill=tk.BOTH, expand=True)
        self.refresh_album_grid(self.albums_data)

    def show_artists(self):
        self.current_view = "artists"
        self.lbl_sidebar_view.config(text="🎤 Artistes")
        self.update_sidebar_highlight("artists")
        self.pl_listbox.selection_clear(0, tk.END)
        self.hide_all_views()
        self.artist_view_frame.pack(fill=tk.BOTH, expand=True)
        self.refresh_artist_tree(self.artists_data)

    def show_queue(self):
        self.current_view = "queue"
        self.lbl_sidebar_view.config(text="▶ File d'attente")
        self.update_sidebar_highlight("queue")
        self.pl_listbox.selection_clear(0, tk.END)
        self.hide_all_views()
        self.queue_view_frame.pack(fill=tk.BOTH, expand=True)
        self.refresh_queue_tree()

    def on_playlist_select(self, event):
        try:
            pl_name = self.pl_listbox.get(self.pl_listbox.curselection()[0])
            self.current_view = pl_name
            self.lbl_sidebar_view.config(text=f"📋 Playlist : {pl_name}")
            self.update_sidebar_highlight(pl_name)  # aucune correspondance -> tous les boutons redeviennent neutres
            self.hide_all_views()
            self.track_view_frame.pack(fill=tk.BOTH, expand=True)
            self.refresh_tree([t for t in self.all_tracks if t["path"] in self.playlists.get(pl_name, [])])
        except IndexError: pass

    # --- LOGIQUE FILE D'ATTENTE & DRAG/DROP ---
    def refresh_queue_tree(self):
        self.queue_tree.delete(*self.queue_tree.get_children())
        
        # 1. Musique en cours
        if self.current_track_path:
            track = self.get_track_info(self.current_track_path)
            if track:
                self.queue_tree.insert("", tk.END, values=("🎵 En cours", track["titre"], track["album"], track["annee"]), tags=(track["path"],))
                
        # 2. File d'attente manuelle (Prioritaire)
        for path in self.queue_manual:
            track = self.get_track_info(path)
            if track:
                self.queue_tree.insert("", tk.END, values=("⏭️ Manuelle", track["titre"], track["album"], track["annee"]), tags=(track["path"],))
                
        # 3. File d'attente automatique
        origine = "🔀 Aléatoire" if self.shuffle_mode else "📄 Automatique"
        for path in self.queue_auto:
            track = self.get_track_info(path)
            if track:
                self.queue_tree.insert("", tk.END, values=(origine, track["titre"], track["album"], track["annee"]), tags=(track["path"],))

    def on_queue_press(self, event):
        item = self.queue_tree.identify_row(event.y)
        if item:
            # Empêcher le déplacement de la musique "En cours"
            if self.queue_tree.item(item, "values")[0] == "🎵 En cours":
                self.drag_data = None
                return
            self.drag_data = {"item": item}
        else:
            self.drag_data = None

    def on_queue_release(self, event):
        if getattr(self, 'drag_data', None) is None:
            return
        
        target_item = self.queue_tree.identify_row(event.y)
        drag_item = self.drag_data["item"]
        
        if target_item and target_item != drag_item:
            target_idx = self.queue_tree.index(target_item)
            # Protéger la ligne 0 (En cours de lecture)
            if self.current_track_path and target_idx == 0:
                target_idx = 1
                
            self.queue_tree.move(drag_item, '', target_idx)
            self.sync_queues_from_tree()
            
        self.drag_data = None

    def sync_queues_from_tree(self):
        """Met à jour les listes internes (manuelle / automatique) selon le nouvel ordre visuel,
        en conservant l'origine réelle de chaque morceau — le glisser-déposer réordonne, il ne
        transforme plus tout en 'Manuelle'. Réaffiche ensuite dans l'ordre réel de lecture
        (manuel groupé avant auto) pour que l'affichage ne mente jamais sur ce qui va être joué."""
        new_manual, new_auto = [], []
        for child in self.queue_tree.get_children():
            values = self.queue_tree.item(child, "values")
            if values[0] == "🎵 En cours":
                continue
            track_path = self.queue_tree.item(child, "tags")[0]
            if values[0] == "⏭️ Manuelle":
                new_manual.append(track_path)
            else:
                new_auto.append(track_path)

        self.queue_manual = new_manual
        self.queue_auto = new_auto
        self.refresh_queue_tree()

    # --- LOGIQUE DE LECTURE (NOYAU) ---
    def get_track_info(self, track_path):
        return self.tracks_by_path.get(track_path)

    def play_track(self, track_path, add_to_history=True, override_info=None, is_youtube=False):
        """`override_info` permet de fournir titre/album/durée pour une piste absente de la
        bibliothèque locale (cas des morceaux YouTube mis en cache). `is_youtube` indique si la
        lecture doit, une fois la piste terminée, enchaîner sur le tampon YouTube plutôt que sur
        les files d'attente classiques (voir play_next)."""
        try:
            if add_to_history and self.current_track_path:
                self.history.append(self.current_track_path)

            track_info = self.get_track_info(track_path) or override_info
            self.track_length = track_info["duree"] if track_info else 0
            self.audio.load(track_path)
            self.audio.set_track_length(self.track_length)
            self.audio.play()

            self.current_track_path = track_path
            self.youtube_source_active = is_youtube
            self.progress_set_max(self.track_length if self.track_length > 0 else 100)
            self.lbl_time_total.config(text=self.format_time(self.track_length))
            self.progress_set(0)

            self.paused = False
            self.is_stopped = False
            self.update_play_pause_button()

            if track_info:
                self.lbl_now_playing_title.config(text=track_info["titre"])
                self.lbl_now_playing_album.config(text=f"{track_info['album']} ({track_info['annee']})")
            self.update_cover_art(track_path)
            
            if self.current_view == "queue":
                self.refresh_queue_tree()
                
        except Exception as e:
            logger.error("Erreur de lecture pour %s : %s", track_path, e)

    def on_double_click_track(self):
        selected = self.tree.selection()
        if not selected: return
        track_path = self.tree.item(selected[0], "tags")[0]
        
        # Mettre à jour la file d'attente automatique
        all_items = self.tree.get_children()
        idx = all_items.index(selected[0])
        self._set_upcoming_queue([self.tree.item(child, "tags")[0] for child in all_items[idx+1:]])
        self.queue_manual = [] # Vider la manuelle si on clique directement dans la biblio
        
        self.play_track(track_path)

    def show_album_detail(self, album_name):
        """Affiche la liste des morceaux d'un album pour permettre de choisir précisément ce que l'on veut écouter."""
        if album_name not in self.albums_data: return
        self.current_view = "album_detail"
        self.current_album_name = album_name
        self.current_album_tracks = self.albums_data[album_name]["tracks"]

        infos = self.albums_data[album_name]
        self.lbl_sidebar_view.config(text=f"💿 {album_name}")
        self.update_sidebar_highlight("album_detail")
        self.lbl_album_detail_title.config(text=f"{album_name}  •  {infos['annee']}  •  {infos['genre']}")

        self.pl_listbox.selection_clear(0, tk.END)
        self.hide_all_views()
        self.album_detail_frame.pack(fill=tk.BOTH, expand=True)
        self.refresh_album_detail_tree(self.current_album_tracks)

    def refresh_album_detail_tree(self, tracks):
        self.album_detail_tree.delete(*self.album_detail_tree.get_children())
        for track in tracks:
            self.album_detail_tree.insert("", tk.END, values=(track["titre"], track["genre"], track["annee"]), tags=(track["path"],))

    def on_double_click_album_track(self):
        """Double-clic sur un morceau dans le détail d'un album : le joue, la suite de l'album passe en file automatique."""
        selected = self.album_detail_tree.selection()
        if not selected: return
        track_path = self.album_detail_tree.item(selected[0], "tags")[0]

        all_items = self.album_detail_tree.get_children()
        idx = all_items.index(selected[0])
        self._set_upcoming_queue([self.album_detail_tree.item(child, "tags")[0] for child in all_items[idx+1:]])
        self.queue_manual = []

        self.play_track(track_path)

    def play_album_from_start(self, album_name):
        """Lance la lecture d'un album complet depuis sa première piste (utilisé par le bouton Lecture global)."""
        tracks = self.albums_data.get(album_name, {}).get("tracks", [])
        if not tracks: return
        self._set_upcoming_queue([t["path"] for t in tracks[1:]])
        self.queue_manual = []
        self.play_track(tracks[0]["path"])

    def on_double_click_artist(self):
        """Double-clic sur un artiste : ouvre la vue détaillée listant tous ses morceaux."""
        selected = self.artist_tree.selection()
        if not selected: return
        artist_key = self.artist_tree.item(selected[0], "tags")[0]
        self.show_artist_detail(artist_key)

    def show_artist_detail(self, artist_key):
        """Affiche la liste des morceaux d'un artiste (triable par année de sortie, album, genre...)."""
        if artist_key not in self.artists_data: return
        self.current_view = "artist_detail"
        self.current_artist_name = artist_key   # clé normalisée, utilisée pour retrouver artists_data
        self.current_artist_tracks = self.artists_data[artist_key]["tracks"]
        display_name = self.artists_data[artist_key]["display"]

        self.lbl_sidebar_view.config(text=f"🎤 {display_name}")
        self.update_sidebar_highlight("artist_detail")
        self.lbl_artist_detail_title.config(text=f"{display_name}  •  {len(self.current_artist_tracks)} titre(s)")

        self.pl_listbox.selection_clear(0, tk.END)
        self.hide_all_views()
        self.artist_detail_frame.pack(fill=tk.BOTH, expand=True)
        self.refresh_artist_detail_tree(self.current_artist_tracks)

    def refresh_artist_detail_tree(self, tracks):
        self.artist_detail_tree.delete(*self.artist_detail_tree.get_children())
        for track in tracks:
            self.artist_detail_tree.insert("", tk.END, values=(track["titre"], track["album"], track["genre"], track["annee"]), tags=(track["path"],))

    def on_double_click_artist_track(self):
        """Double-clic sur un morceau dans le détail d'un artiste : le joue, la suite passe en file automatique."""
        selected = self.artist_detail_tree.selection()
        if not selected: return
        track_path = self.artist_detail_tree.item(selected[0], "tags")[0]

        all_items = self.artist_detail_tree.get_children()
        idx = all_items.index(selected[0])
        self._set_upcoming_queue([self.artist_detail_tree.item(child, "tags")[0] for child in all_items[idx+1:]])
        self.queue_manual = []

        self.play_track(track_path)

    def play_artist_from_start(self, artist_name):
        """Lance la lecture de tous les morceaux d'un artiste depuis le premier (utilisé par le bouton Lecture global)."""
        tracks = self.artists_data.get(artist_name, {}).get("tracks", [])
        if not tracks: return
        self._set_upcoming_queue([t["path"] for t in tracks[1:]])
        self.queue_manual = []
        self.play_track(tracks[0]["path"])

    def on_double_click_queue(self, event):
        selected = self.queue_tree.selection()
        if not selected: return
        
        item = selected[0]
        values = self.queue_tree.item(item, "values")
        if values[0] == "🎵 En cours": return # Déjà en lecture
            
        track_path = self.queue_tree.item(item, "tags")[0]
        all_items = self.queue_tree.get_children()
        idx = all_items.index(item)
        
        # Tout ce qui se trouve APRÈS ce clic devient la nouvelle file d'attente manuelle
        self.queue_manual = [self.queue_tree.item(child, "tags")[0] for child in all_items[idx+1:]]
        self.queue_auto = []
        
        self.play_track(track_path)

    def btn_play_clicked(self):
        if self.current_view == "albums":
            if self._selected_albums:
                self.play_album_from_start(next(iter(self._selected_albums)))
        elif self.current_view == "album_detail":
            if self.album_detail_tree.selection():
                self.on_double_click_album_track()
            elif self.current_album_name:
                self.play_album_from_start(self.current_album_name)
        elif self.current_view == "artists":
            selected = self.artist_tree.selection()
            if selected:
                artist_name = self.artist_tree.item(selected[0], "tags")[0]
                self.play_artist_from_start(artist_name)
        elif self.current_view == "artist_detail":
            if self.artist_detail_tree.selection():
                self.on_double_click_artist_track()
            elif self.current_artist_name:
                self.play_artist_from_start(self.current_artist_name)
        elif self.current_view == "queue":
            if self.queue_tree.selection():
                self.on_double_click_queue(None)
        else:
            self.on_double_click_track()

    def play_next(self):
        # Répétition : on rejoue le même morceau à l'infini, sans toucher aux files d'attente
        if self.repeat_mode and self.current_track_path:
            self.play_track(self.current_track_path, add_to_history=False)
            return
        if self.queue_manual:
            self.play_track(self.queue_manual.pop(0))
        elif self.queue_auto:
            self.play_track(self.queue_auto.pop(0))
        elif self.shuffle_mode:
            # File aléatoire épuisée : on repioche un nouveau lot mélangé plutôt que de s'arrêter
            self._generate_shuffle_queue()
            if self.queue_auto:
                self.play_track(self.queue_auto.pop(0))
            else:
                self.stop()
        elif self.youtube_source_active:
            # Radio YouTube : la piste courante venait du tampon, on enchaîne dessus plutôt
            # que de s'arrêter (voir play_next_from_youtube_buffer).
            self.play_next_from_youtube_buffer()
        else:
            self.stop()

    # --- YOUTUBE : LECTURE DEPUIS LE TAMPON (voir YoutubeTab / YouTubeManager) ---
    def play_next_from_youtube_buffer(self, manual=False, retries_left=5):
        """Tire le prochain morceau prêt dans le tampon YouTube et le joue. Utilisé à la fois
        pour l'enchaînement automatique (fin de piste, voir play_next) et pour le bouton
        « Lancer la radio » de l'onglet YouTube (manual=True).
        Si le tampon est momentanément vide (téléchargement encore en cours) lors d'un
        enchaînement automatique, on retente quelques fois avant d'abandonner, plutôt que de
        couper la lecture pour un simple ralentissement réseau."""
        manager = self.youtube_manager
        if not manager:
            if manual:
                messagebox.showinfo("YouTube", "Connecte-toi d'abord depuis l'onglet YouTube.", parent=self.root)
            else:
                self.stop()
            return

        track = manager.get_next_online_track()
        if not track:
            if manual:
                messagebox.showinfo("YouTube", "Le tampon est encore vide, patiente quelques secondes...", parent=self.root)
            elif retries_left > 0:
                self.root.after(1500, lambda: self.play_next_from_youtube_buffer(retries_left=retries_left - 1))
            else:
                self.stop()
            return

        override_info = {"titre": track["titre"], "album": "YouTube Music (radio)", "annee": "", "duree": 0}
        self.queue_manual = []
        self.queue_auto = []
        self.play_track(track["chemin_local"], override_info=override_info, is_youtube=True)
        self._update_youtube_cover(None)  # pas de vignette dispo depuis le tampon -> pochette par défaut

    def play_youtube_item_now(self, item):
        """Télécharge (si nécessaire) puis joue immédiatement un morceau choisi dans la Sélection
        rapide YouTube. Appelé depuis un thread d'arrière-plan par YoutubeTab -- le téléchargement
        se fait ici, mais la lecture elle-même (widgets Tkinter) est replanifiée sur le thread
        principal via root.after, car Tkinter n'est pas thread-safe.
        Retourne un message d'erreur (str) en cas d'échec, ou None si tout s'est bien passé."""
        manager = self.youtube_manager
        if not manager:
            return "Non connecté à YouTube Music."
        video_id = item.get("video_id")
        if not video_id:
            return "Morceau invalide (pas d'identifiant vidéo)."

        titre = item.get("titre", "Titre inconnu")
        lien = item.get("lien") or f"https://music.youtube.com/watch?v={video_id}"
        safe_title = "".join(c for c in titre if c.isalpha() or c.isdigit() or c == " ").strip()[:80]
        filepath = os.path.join(manager.temp_dir, f"{safe_title}_{video_id}.mp3")

        if not os.path.exists(filepath):
            try:
                ok = manager._download_track(lien, filepath)
            except Exception as e:
                return str(e)
            if not ok:
                return f"Impossible de télécharger « {titre} » (voir les logs pour le détail)."

        override_info = {"titre": titre, "album": "YouTube Music", "annee": "", "duree": 0}
        self.root.after(0, lambda: self._start_youtube_playback(filepath, override_info, item))
        return None

    def _start_youtube_playback(self, filepath, override_info, item):
        self.queue_manual = []
        self.queue_auto = []
        self.play_track(filepath, override_info=override_info, is_youtube=True)
        self._update_youtube_cover(item.get("vignette"))

    def _update_youtube_cover(self, url):
        """Affiche la vignette YouTube du morceau en cours comme pochette, sinon la pochette
        par défaut. Téléchargement en arrière-plan pour ne pas geler la GUI."""
        if not url:
            self.set_placeholder_cover()
            return

        def worker():
            try:
                resp = requests.get(url, timeout=8)
                img = Image.open(io.BytesIO(resp.content)).resize((220, 220), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(img, master=self.root)

                def apply():
                    self.cover_label.config(image=photo)
                    self.current_cover = photo
                self.root.after(0, apply)
            except Exception:
                self.root.after(0, self.set_placeholder_cover)

        threading.Thread(target=worker, daemon=True).start()

    def _generate_shuffle_queue(self):
        """Génère un lot de morceaux mélangés et le place dans la file automatique, pour que
        l'onglet File d'attente montre réellement ce qui va être joué en mode Aléatoire
        (au lieu de piocher un morceau au hasard au dernier moment, invisible pour l'utilisateur)."""
        candidates = [t["path"] for t in self.all_tracks if t["path"] != self.current_track_path]
        if not candidates:
            candidates = [t["path"] for t in self.all_tracks]
        random.shuffle(candidates)
        self.queue_auto = candidates
        if self.current_view == "queue":
            self.refresh_queue_tree()

    def _set_upcoming_queue(self, sequential_tracks):
        """Définit la file automatique pour la suite de la lecture. Si le mode aléatoire est actif,
        on repioche un lot mélangé plutôt que d'utiliser l'ordre séquentiel fourni, pour que la
        file d'attente affichée reste cohérente avec ce qui sera vraiment joué ensuite."""
        if self.shuffle_mode:
            self._generate_shuffle_queue()
        else:
            self.queue_auto = sequential_tracks

    def play_prev(self):
        # Si la musique est avancée de plus de 3 secondes, on la recommence
        if self.audio.get_position() > 3.0:
            self.audio.rewind()
            self.progress_set(0)
            return
            
        # Sinon, on joue la vraie musique précédente via l'historique
        if self.history:
            prev_track = self.history.pop()
            if self.current_track_path:
                self.queue_manual.insert(0, self.current_track_path) # Repousse l'actuelle dans la file
            self.play_track(prev_track, add_to_history=False)

    def toggle_shuffle(self):
        """Active/désactive la lecture aléatoire. Mutuellement exclusif avec la répétition."""
        self.shuffle_mode = not self.shuffle_mode
        if self.shuffle_mode and self.repeat_mode:
            self.repeat_mode = False
            self.btn_repeat.config(image=self.icons["repeat_off"])
        self.btn_shuffle.config(image=self.icons["shuffle_on" if self.shuffle_mode else "shuffle_off"])
        if self.shuffle_mode:
            self._generate_shuffle_queue()  # remplit tout de suite la file avec un lot mélangé visible
        self._update_mode_label()

    def toggle_repeat(self):
        """Active/désactive la répétition du morceau en cours. Mutuellement exclusif avec l'aléatoire."""
        self.repeat_mode = not self.repeat_mode
        if self.repeat_mode and self.shuffle_mode:
            self.shuffle_mode = False
            self.btn_shuffle.config(image=self.icons["shuffle_off"])
        self.btn_repeat.config(image=self.icons["repeat_on" if self.repeat_mode else "repeat_off"])
        self._update_mode_label()

    def _update_mode_label(self):
        """Affiche en toutes lettres le mode de lecture actif (au cas où la couleur des icônes
        ne suffise pas à voir clairement lequel des deux est enclenché)."""
        if self.shuffle_mode:
            self.lbl_playback_mode.config(text="🔀 Lecture aléatoire activée")
        elif self.repeat_mode:
            self.lbl_playback_mode.config(text="🔁 Répétition du morceau activée")
        else:
            self.lbl_playback_mode.config(text="")

    def build_progress_slider(self, parent):
        """Barre de progression dessinée sur mesure : la partie déjà écoutée est remplie en bleu
        (impossible avec un ttk.Scale nu, dont le fond est opaque). La logique de seek protégée
        (is_sliding, rechargement de current_track_path) est conservée à l'identique : cette
        fonction ne fait que remplacer l'apparence, pas le comportement documenté dans le README."""
        self._progress_max = 100
        self._progress_value = 0
        height = 20
        self.progress_canvas = tk.Canvas(parent, height=height, bg="#f1f5f9", highlightthickness=0, cursor="hand2")
        self.progress_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=15)

        def redraw():
            c = self.progress_canvas
            c.delete("all")
            w = c.winfo_width()
            if w <= 1:
                return
            y, pad = height // 2, 9
            x0, x1 = pad, max(pad, w - pad)
            ratio = 0 if self._progress_max <= 0 else max(0, min(1, self._progress_value / self._progress_max))
            fill_x = x0 + (x1 - x0) * ratio
            c.create_line(x0, y, x1, y, fill="#cbd5e1", width=6, capstyle=tk.ROUND)
            if fill_x > x0:
                c.create_line(x0, y, fill_x, y, fill="#3b82f6", width=6, capstyle=tk.ROUND)
            r = 8
            c.create_oval(fill_x - r, y - r, fill_x + r, y + r, fill="#3b82f6", outline="#ffffff", width=2)

        self._redraw_progress = redraw

        def x_to_value(x):
            w = self.progress_canvas.winfo_width()
            pad = 9
            x0, x1 = pad, max(pad, w - pad)
            if x1 <= x0:
                return 0
            return max(0, min(1, (x - x0) / (x1 - x0))) * self._progress_max

        def on_press(event):
            self.on_slide_press(event)          # inchangé : is_sliding = True
            self._progress_value = x_to_value(event.x)
            redraw()

        def on_drag(event):
            self._progress_value = x_to_value(event.x)
            redraw()

        self.progress_canvas.bind("<ButtonPress-1>", on_press)
        self.progress_canvas.bind("<B1-Motion>", on_drag)
        self.progress_canvas.bind("<ButtonRelease-1>", self.on_slide_release)  # inchangé : lit progress_get()
        self.progress_canvas.bind("<Configure>", lambda e: redraw())
        redraw()

    def progress_set(self, value):
        """Équivalent de l'ancien progress_scale.set(value)."""
        self._progress_value = value
        if hasattr(self, "_redraw_progress"):
            self._redraw_progress()

    def progress_get(self):
        """Équivalent de l'ancien progress_scale.get()."""
        return self._progress_value

    def progress_set_max(self, value):
        """Équivalent de l'ancien progress_scale.config(to=value)."""
        self._progress_max = value if value > 0 else 100
        if hasattr(self, "_redraw_progress"):
            self._redraw_progress()

    def build_volume_slider(self, parent, width=100, height=20):
        """Curseur de volume dessiné sur mesure : piste grise + remplissage bleu jusqu'à la poignée
        (contrairement à un ttk.Scale nu, le niveau se lit d'un coup d'œil). Clic direct ou glissé."""
        pad, self._vol_track_y = 8, height // 2
        self.volume_canvas = tk.Canvas(parent, width=width + pad * 2, height=height, bg="#f1f5f9", highlightthickness=0, cursor="hand2")
        self.volume_canvas.pack(side=tk.LEFT)
        self._vol_x0, self._vol_x1 = pad, width + pad

        def value_to_x(v):
            return self._vol_x0 + (self._vol_x1 - self._vol_x0) * (max(0, min(100, v)) / 100)

        def x_to_value(x):
            return max(0, min(100, (x - self._vol_x0) / (self._vol_x1 - self._vol_x0) * 100))

        self._volume_value_to_x = value_to_x

        def redraw():
            c = self.volume_canvas
            c.delete("all")
            y = self._vol_track_y
            v = self.volume_var.get()
            handle_x = value_to_x(v)
            c.create_line(self._vol_x0, y, self._vol_x1, y, fill="#cbd5e1", width=4, capstyle=tk.ROUND)
            if handle_x > self._vol_x0:
                c.create_line(self._vol_x0, y, handle_x, y, fill="#3b82f6", width=4, capstyle=tk.ROUND)
            r = 6
            c.create_oval(handle_x - r, y - r, handle_x + r, y + r, fill="#3b82f6", outline="#ffffff", width=2)

        self._redraw_volume_slider = redraw

        def on_interact(event):
            self.set_volume(x_to_value(event.x))

        self.volume_canvas.bind("<Button-1>", on_interact)
        self.volume_canvas.bind("<B1-Motion>", on_interact)
        redraw()

    def set_volume(self, value):
        """Point d'entrée unique pour changer le volume : met à jour le moteur audio, la variable et le dessin du curseur."""
        value = max(0, min(100, value))
        self.volume_var.set(value)
        self.audio.set_volume(value / 100)
        if value > 0:
            self._pre_mute_volume = value
        self.lbl_volume_icon.config(image=self.icons["volume_muted"] if value == 0 else self.icons["volume"])
        if hasattr(self, "_redraw_volume_slider"):
            self._redraw_volume_slider()

    def on_volume_change(self, value):
        """Conservé pour compatibilité (curseur historique) : redirige vers set_volume."""
        self.set_volume(float(value))

    def toggle_mute(self, event=None):
        """Clic sur l'icône haut-parleur : coupe le son, ou restaure le dernier volume non nul."""
        if self.volume_var.get() > 0:
            self.set_volume(0)
        else:
            self.set_volume(self._pre_mute_volume or 70)

    # --- MENUS CLIC DROIT ---
    def get_active_tree(self):
        """Retourne le Treeview actuellement visible à l'écran (Musiques, Artistes ou leurs détails).
        La vue Albums n'est PAS un Treeview (grille de cartes, voir _selected_albums)."""
        if self.current_view == "album_detail":
            return self.album_detail_tree
        elif self.current_view == "artists":
            return self.artist_tree
        elif self.current_view == "artist_detail":
            return self.artist_detail_tree
        else:
            return self.tree

    def show_context_menu(self, event):
        if self.current_view == "albums":
            if not self._selected_albums: return
        else:
            active_tree = self.get_active_tree()
            iid = active_tree.identify_row(event.y)
            if not iid: return
            if iid not in active_tree.selection(): active_tree.selection_set(iid)
        
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="▶ Ajouter à la file d'attente", command=self.add_to_queue)
        menu.add_separator()

        playlist_submenu = tk.Menu(menu, tearoff=0)
        for pl_name in self.playlists.keys():
            playlist_submenu.add_command(label=pl_name, command=lambda name=pl_name: self.add_selected_to_playlist(name))
        playlist_submenu.add_separator()
        playlist_submenu.add_command(label="➕ Créer...", command=self.create_playlist)
        menu.add_cascade(label="Ajouter à la playlist", menu=playlist_submenu)

        if self.current_view not in ["library", "albums", "queue", "album_detail", "artists", "artist_detail"]:
            menu.add_separator()
            menu.add_command(label="❌ Retirer", command=self.remove_selected_from_playlist)
        menu.post(event.x_root, event.y_root)

    def add_to_queue(self):
        added_count = 0
        if self.current_view == "albums":
            for album_name in self._selected_albums:
                for track in self.albums_data[album_name]["tracks"]:
                    self.queue_manual.append(track["path"])
                    added_count += 1
        else:
            active_tree = self.get_active_tree()
            selected_items = active_tree.selection()
            if not selected_items: return
            if self.current_view == "artists":
                added_paths = set()
                for item in selected_items:
                    artist_key = active_tree.item(item, "tags")[0]
                    for track in self.artists_data[artist_key]["tracks"]:
                        if track["path"] not in added_paths:  # évite les doublons (featuring compté sous 2 artistes sélectionnés)
                            added_paths.add(track["path"])
                            self.queue_manual.append(track["path"])
                            added_count += 1
            else:
                for item in selected_items:
                    self.queue_manual.append(active_tree.item(item, "tags")[0])
                    added_count += 1
                
        messagebox.showinfo("File d'attente", f"{added_count} musiques ajoutées à la file d'attente !")
        if self.current_view == "queue":
            self.refresh_queue_tree()

    # --- AUTRES LOGIQUES EXISTANTES ---
    def on_drag_select(self, event, tree_widget):
        item = tree_widget.identify_row(event.y)
        if item: tree_widget.selection_add(item)

    def build_albums_data(self):
        self.albums_data = {}
        for track in self.all_tracks:
            album_name = track.get("album", "Inconnu")
            if album_name not in self.albums_data:
                self.albums_data[album_name] = {"annee": track.get("annee", "Inconnu"), "genre": track.get("genre", "Inconnu"), "cover_path": track["path"], "tracks": []}
            self.albums_data[album_name]["tracks"].append(track)

    def build_artists_data(self):
        """Regroupe les morceaux par artiste individuel (un morceau en collaboration apparaît
        sous chacun de ses artistes), en fusionnant les variantes de casse/séparateurs
        ('Feat.', 'ft', 'featuring', virgule, '&'...) grâce à une clé normalisée en minuscules."""
        self.artists_data = {}
        for track in self.all_tracks:
            for name in parse_artist_names(track.get("artiste", "Inconnu")):
                key = name.lower()
                if key not in self.artists_data:
                    self.artists_data[key] = {"display": name, "genre": track.get("genre", "Inconnu"), "tracks": []}
                elif name[0].isupper() and not self.artists_data[key]["display"][0].isupper():
                    # On préfère garder une variante correctement capitalisée comme nom affiché
                    self.artists_data[key]["display"] = name
                self.artists_data[key]["tracks"].append(track)

    def build_track_index(self):
        """Index chemin -> morceau, pour un accès en O(1) au lieu de reparcourir toute la bibliothèque à chaque appel."""
        self.tracks_by_path = {t["path"]: t for t in self.all_tracks}

    def refresh_artist_tree(self, artists_dict):
        self.artist_tree.delete(*self.artist_tree.get_children())
        for key, data in artists_dict.items():
            self.artist_tree.insert("", tk.END, values=(data["display"], len(data["tracks"]), data["genre"]), tags=(key,))

    def _get_album_cover(self, album_name, data):
        """Retourne la pochette (mise en cache) d'un album pour la grille, ou une icône par défaut."""
        if not hasattr(self, 'default_album_icon'):
            img = Image.new('RGB', (130, 130), color='#334155')
            draw = ImageDraw.Draw(img)
            draw.ellipse([35, 35, 95, 95], outline='#475569', width=3)
            self.default_album_icon = ImageTk.PhotoImage(img, master=self.root)
        if album_name in self.album_covers_cache:
            return self.album_covers_cache[album_name]
        cover_img = self.default_album_icon
        try:
            tag = TinyTag.get(data["cover_path"], image=True)
            if tag.get_image():
                img = Image.open(io.BytesIO(tag.get_image())).resize((130, 130), Image.Resampling.LANCZOS)
                cover_img = ImageTk.PhotoImage(img, master=self.root)
        except Exception:
            pass
        self.album_covers_cache[album_name] = cover_img  # mis en cache même la pochette par défaut (évite de relire le tag à chaque redessin)
        return cover_img

    def _on_album_canvas_resize(self, event):
        """Recalcule le nombre de colonnes selon la largeur disponible et ne reconstruit la grille
        que si ce nombre a réellement changé (évite de tout redessiner à chaque pixel de redimensionnement)."""
        self.album_canvas.itemconfig(self._album_grid_window, width=event.width)
        card_total_width = self._ALBUM_CARD_W + self._ALBUM_CARD_PAD * 2
        new_cols = max(1, event.width // card_total_width)
        if new_cols != self._album_grid_cols:
            self._album_grid_cols = new_cols
            self.refresh_album_grid(self._albums_dict_shown)

    def refresh_album_grid(self, albums_dict):
        """(Re)construit la grille de pochettes, triée selon le critère choisi dans la barre d'outils."""
        self._albums_dict_shown = albums_dict
        for child in self.album_grid_frame.winfo_children():
            child.destroy()
        self._album_cards = {}

        sort_key = self.album_sort_var.get()
        items = list(albums_dict.items())
        if sort_key == "Année":
            items.sort(key=lambda kv: (kv[1]["annee"] == "Inconnu", kv[1]["annee"]))
        elif sort_key == "Genre":
            items.sort(key=lambda kv: kv[1]["genre"].lower())
        else:
            items.sort(key=lambda kv: kv[0].lower())

        cols = self._album_grid_cols
        for idx, (album_name, data) in enumerate(items):
            card = self._build_album_card(self.album_grid_frame, album_name, data)
            r, c = divmod(idx, cols)
            card.grid(row=r, column=c, padx=self._ALBUM_CARD_PAD, pady=self._ALBUM_CARD_PAD, sticky="n")
            self._album_cards[album_name] = card

        # Les cartes recouvrent quasi toute la zone visible : sans ce binding récursif, la molette
        # ne fonctionnerait que dans les interstices entre cartes (le canvas ne reçoit l'événement
        # que là où aucun widget enfant ne le "capte" avant lui).
        self._bind_mousewheel_recursive(self.album_grid_frame)

        self.album_canvas.after(10, lambda: self.album_canvas.configure(scrollregion=self.album_canvas.bbox("all")))

    def _on_album_mousewheel(self, event):
        """Molette Windows/macOS (delta positif = vers le haut, négatif = vers le bas)."""
        self.album_canvas.yview_scroll(-3 if event.delta > 0 else 3, "units")

    def _bind_mousewheel_recursive(self, widget):
        """Bind la molette sur `widget` ET récursivement sur tous ses enfants, pour que le
        défilement fonctionne peu importe où la souris survole dans la grille de cartes."""
        widget.bind("<MouseWheel>", self._on_album_mousewheel)
        widget.bind("<Button-4>", lambda e: self.album_canvas.yview_scroll(-3, "units"))
        widget.bind("<Button-5>", lambda e: self.album_canvas.yview_scroll(3, "units"))
        for child in widget.winfo_children():
            self._bind_mousewheel_recursive(child)

    def _build_album_card(self, parent, album_name, data):
        """Construit une carte d'album (pochette + nom + année) cliquable : clic = sélection
        (Ctrl+clic = multi-sélection), double-clic = ouvre le détail, clic droit = menu contextuel."""
        cover_img = self._get_album_cover(album_name, data)
        selected = album_name in self._selected_albums
        bg = "#eff6ff" if selected else "#ffffff"
        border = "#3b82f6" if selected else "#e2e8f0"

        card = tk.Frame(parent, bg=bg, width=self._ALBUM_CARD_W, height=228,
                         highlightthickness=2, highlightbackground=border, highlightcolor=border, cursor="hand2")
        card.pack_propagate(False)

        cover_label = tk.Label(card, image=cover_img, bg=bg)
        cover_label.image = cover_img  # référence conservée (sinon PhotoImage garbage-collecté)
        cover_label.pack(pady=(12, 8))

        name_label = tk.Label(card, text=album_name, font=(self.font_family, 10, "bold"), bg=bg,
                               fg="#1e293b", wraplength=self._ALBUM_CARD_W - 20, justify="center")
        name_label.pack(padx=10)

        sub_label = tk.Label(card, text=data.get("annee", "Inconnu"), font=(self.font_family, 9), bg=bg, fg="#64748b")
        sub_label.pack(pady=(2, 10))

        for widget in (card, cover_label, name_label, sub_label):
            widget.bind("<Button-1>", lambda e, a=album_name: self._on_album_card_click(e, a))
            widget.bind("<Double-1>", lambda e, a=album_name: self.show_album_detail(a))
            widget.bind("<Button-3>", lambda e, a=album_name: self._on_album_card_right_click(e, a))
        return card

    def _on_album_card_click(self, event, album_name):
        """Clic simple = sélectionne uniquement cet album ; Ctrl+clic = ajoute/retire de la sélection."""
        if event.state & 0x4:  # touche Ctrl enfoncée
            self._selected_albums.symmetric_difference_update({album_name})
        else:
            self._selected_albums = {album_name}
        self._refresh_album_card_selection_visuals()

    def _on_album_card_right_click(self, event, album_name):
        if album_name not in self._selected_albums:
            self._selected_albums = {album_name}
            self._refresh_album_card_selection_visuals()
        self.show_context_menu(event)

    def _refresh_album_card_selection_visuals(self):
        """Met à jour l'apparence (fond + liseré) de chaque carte selon l'état de sélection courant."""
        for name, card in self._album_cards.items():
            selected = name in self._selected_albums
            bg = "#eff6ff" if selected else "#ffffff"
            border = "#3b82f6" if selected else "#e2e8f0"
            card.config(bg=bg, highlightbackground=border, highlightcolor=border)
            for child in card.winfo_children():
                child.config(bg=bg)

    def refresh_tree(self, tracks):
        self.current_tracks = tracks
        self.tree.delete(*self.tree.get_children())
        for track in tracks:
            self.tree.insert("", tk.END, values=(track["titre"], track.get("artiste", "Inconnu"), track["album"], track["genre"], track["annee"]), tags=(track["path"],))

    def filter_music(self, *args):
        query = self.search_var.get().lower()
        scope = self.search_scope_var.get()

        def track_matches(t):
            """Teste un morceau selon le champ choisi en recherche avancée (ou tous les champs par défaut)."""
            if scope == "Titre":
                return query in t["titre"].lower()
            elif scope == "Artiste":
                return query in t.get("artiste", "Inconnu").lower()
            elif scope == "Album":
                return query in t["album"].lower()
            elif scope == "Année":
                return query in t["annee"].lower()
            else:  # "Tous"
                return (query in t["titre"].lower() or query in t.get("artiste", "Inconnu").lower()
                        or query in t["album"].lower() or query in t["genre"].lower() or query in t["annee"].lower())

        if self.current_view == "albums":
            if not query: return self.refresh_album_grid(self.albums_data)
            self.refresh_album_grid({k: v for k, v in self.albums_data.items() if query in k.lower() or query in v["genre"].lower() or query in v["annee"]})
        elif self.current_view == "artists":
            if not query: return self.refresh_artist_tree(self.artists_data)
            self.refresh_artist_tree({k: v for k, v in self.artists_data.items() if query in v["display"].lower() or query in v["genre"].lower()})
        elif self.current_view == "queue":
            pass # On ne filtre pas la file d'attente pour éviter les bugs de drag & drop
        elif self.current_view == "album_detail":
            if not query: return self.refresh_album_detail_tree(self.current_album_tracks)
            self.refresh_album_detail_tree([t for t in self.current_album_tracks if track_matches(t)])
        elif self.current_view == "artist_detail":
            if not query: return self.refresh_artist_detail_tree(self.current_artist_tracks)
            self.refresh_artist_detail_tree([t for t in self.current_artist_tracks if track_matches(t)])
        else:
            source = self.all_tracks if self.current_view == "library" else [t for t in self.all_tracks if t["path"] in self.playlists.get(self.current_view, [])]
            if not query: return self.refresh_tree(source)
            self.refresh_tree([t for t in source if track_matches(t)])

    def toggle_advanced_search(self):
        """Affiche/masque le sélecteur de champ (Titre/Artiste/Album/Année) pour restreindre la recherche."""
        if self.search_scope_combo.winfo_ismapped():
            self.search_scope_combo.pack_forget()
            self.search_scope_var.set("Tous")
            self.btn_advanced_search.config(bg="#64748b")
            self.filter_music()
        else:
            self.search_scope_combo.pack(side=tk.LEFT, padx=(0, 5), before=self.btn_advanced_search)
            self.btn_advanced_search.config(bg="#3b82f6")

    def sort_column(self, tree, col, reverse):
        l = [(tree.set(k, col), k) for k in tree.get_children('')]
        try: l.sort(key=lambda t: (t[0] == "Inconnu", int(t[0]) if t[0] != "Inconnu" else 0), reverse=reverse)
        except ValueError: l.sort(reverse=reverse)
        for index, (val, k) in enumerate(l): tree.move(k, '', index)
        tree.heading(col, command=lambda: self.sort_column(tree, col, not reverse))

    def load_playlists(self):
        if os.path.exists(PLAYLISTS_FILE):
            try:
                with open(PLAYLISTS_FILE, "r", encoding="utf-8") as f: return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Fichier de playlists illisible (%s), on repart d'une liste vide.", e)
        return {}

    def save_playlists(self):
        with open(PLAYLISTS_FILE, "w", encoding="utf-8") as f: json.dump(self.playlists, f, ensure_ascii=False, indent=4)

    def clean_playlists(self):
        """Retire des playlists les morceaux dont le fichier n'existe plus sur le disque (déplacé/supprimé)."""
        changed = False
        for name, paths in self.playlists.items():
            valid_paths = [p for p in paths if os.path.exists(p)]
            if len(valid_paths) != len(paths):
                self.playlists[name] = valid_paths
                changed = True
        if changed:
            self.save_playlists()

    def load_library_cache(self):
        """Charge la bibliothèque déjà scannée depuis le cache local, pour éviter un nouveau scan à chaque démarrage."""
        if os.path.exists(LIBRARY_CACHE_FILE):
            try:
                with open(LIBRARY_CACHE_FILE, "r", encoding="utf-8") as f:
                    tracks = json.load(f)
                # On ignore silencieusement les fichiers qui n'existent plus (déplacés/supprimés depuis le dernier scan)
                valid_tracks = [t for t in tracks if os.path.exists(t["path"])]

                # Migration : un cache créé avant l'ajout du champ "artiste" ne l'a pas.
                # On relit le vrai tag sur le disque (une seule fois) au lieu de mettre "Inconnu" à tort.
                migrated = False
                for t in valid_tracks:
                    if "artiste" not in t:
                        migrated = True
                        try:
                            tag = TinyTag.get(t["path"])
                            t["artiste"] = tag.artist if tag.artist else "Inconnu"
                        except Exception:
                            t["artiste"] = "Inconnu"
                if migrated:
                    try:
                        with open(LIBRARY_CACHE_FILE, "w", encoding="utf-8") as f:
                            json.dump(valid_tracks, f, ensure_ascii=False, indent=4)
                    except Exception:
                        pass

                return valid_tracks
            except Exception:
                return []
        return []

    def save_library_cache(self):
        """Sauvegarde la bibliothèque scannée pour un chargement instantané au prochain démarrage."""
        try:
            with open(LIBRARY_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.all_tracks, f, ensure_ascii=False, indent=4)
        except Exception:
            pass

    def clear_library(self):
        """Vide la bibliothèque en mémoire et supprime le cache local (les playlists ne sont pas affectées)."""
        if not self.all_tracks:
            return
        if not messagebox.askyesno("Confirmation", "Vider toute la bibliothèque musicale ?\n(Les playlists ne seront pas affectées)"):
            return
        self.all_tracks = []
        self.tracks_by_path = {}
        self.albums_data = {}
        self.album_covers_cache = {}
        self.artists_data = {}
        self._selected_albums = set()
        self._album_cards = {}
        self._albums_dict_shown = {}
        if os.path.exists(LIBRARY_CACHE_FILE):
            try: os.remove(LIBRARY_CACHE_FILE)
            except Exception: pass
        self.show_library()

    def refresh_playlists_listbox(self):
        self.pl_listbox.delete(0, tk.END)
        for pl_name in self.playlists.keys(): self.pl_listbox.insert(tk.END, pl_name)

    def create_playlist(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Créer"); dialog.geometry("300x120")
        entry = tk.Entry(dialog, font=(self.font_family, 10), width=25); entry.pack(pady=15); entry.focus_set()
        def submit():
            name = entry.get().strip()
            if name and name not in self.playlists:
                self.playlists[name] = []; self.save_playlists(); self.refresh_playlists_listbox(); dialog.destroy()
        tk.Button(dialog, text="Valider", bg="#10b981", fg="white", command=submit).pack()

    def delete_playlist(self):
        try:
            pl_name = self.pl_listbox.get(self.pl_listbox.curselection()[0])
            if messagebox.askyesno("Confirmation", f"Supprimer '{pl_name}' ?"):
                del self.playlists[pl_name]; self.save_playlists(); self.refresh_playlists_listbox(); self.show_library()
        except IndexError: pass

    def add_selected_to_playlist(self, playlist_name):
        added_count = 0
        if self.current_view == "albums":
            for album_name in self._selected_albums:
                for track in self.albums_data[album_name]["tracks"]:
                    if track["path"] not in self.playlists[playlist_name]:
                        self.playlists[playlist_name].append(track["path"]); added_count += 1
        else:
            active_tree = self.get_active_tree()
            selected_items = active_tree.selection()
            if not selected_items: return
            if self.current_view == "artists":
                for item in selected_items:
                    for track in self.artists_data[active_tree.item(item, "tags")[0]]["tracks"]:
                        if track["path"] not in self.playlists[playlist_name]:
                            self.playlists[playlist_name].append(track["path"]); added_count += 1
            else:
                for item in selected_items:
                    track_path = active_tree.item(item, "tags")[0]
                    if track_path not in self.playlists[playlist_name]:
                        self.playlists[playlist_name].append(track_path); added_count += 1
        if added_count > 0: self.save_playlists(); messagebox.showinfo("Playlist", f"{added_count} ajout(s) à '{playlist_name}' !")

    def remove_selected_from_playlist(self):
        selected = self.tree.selection()
        if not selected: return
        for item in selected:
            track_path = self.tree.item(item, "tags")[0]
            if track_path in self.playlists[self.current_view]:
                self.playlists[self.current_view].remove(track_path)
        self.save_playlists()
        self.refresh_tree([t for t in self.all_tracks if t["path"] in self.playlists[self.current_view]])

    # --- ICÔNES RONDES DE LA BARRE DE CONTRÔLE ---
    # Dessinées vectoriellement (Pillow) plutôt qu'en texte/emoji : rendu identique sur toutes les
    # machines, quelles que soient les polices installées. Sur-échantillonnage x4 puis réduction
    # avec lissage (LANCZOS) pour des bords nets sans crénelage.
    def _lighten(self, hex_color, amount=0.18):
        """Éclaircit une couleur hexadécimale (utilisé pour l'état survol des boutons)."""
        hex_color = hex_color.lstrip('#')
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
        r = min(255, int(r + (255 - r) * amount))
        g = min(255, int(g + (255 - g) * amount))
        b = min(255, int(b + (255 - b) * amount))
        return f"#{r:02x}{g:02x}{b:02x}"

    def _arrowhead(self, draw, tip, angle_rad, length, color):
        """Dessine une pointe de flèche triangulaire dont la POINTE est exactement au point `tip`
        (coordonnées réelles de fin d'arc), orientée selon `angle_rad` (direction de la tangente)."""
        back = (tip[0] - length * math.cos(angle_rad), tip[1] - length * math.sin(angle_rad))
        perp = angle_rad + math.pi / 2
        half_w = length * 0.55
        p1 = (back[0] + half_w * math.cos(perp), back[1] + half_w * math.sin(perp))
        p2 = (back[0] - half_w * math.cos(perp), back[1] - half_w * math.sin(perp))
        draw.polygon([tip, p1, p2], fill=color)

    def _make_icon(self, kind, diameter, bg_hex, fg_hex="#ffffff"):
        """Génère une icône ronde (pastille bg_hex + glyphe fg_hex) pour un contrôle de lecture.
        bg_hex=None -> pas de pastille dessinée (glyphe seul), pour flotter sur un bouton dont
        le fond change selon l'état (ex. boutons de nav de la sidebar)."""
        scale = 4
        size = diameter * scale
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if bg_hex is not None:
            d.ellipse([0, 0, size - 1, size - 1], fill=bg_hex)
        w = max(2, int(size * 0.06))

        if kind == "play":
            d.polygon([(size * 0.36, size * 0.24), (size * 0.36, size * 0.76), (size * 0.78, size * 0.5)], fill=fg_hex)
        elif kind == "pause":
            bw, gap, top, bot, cx = size * 0.10, size * 0.08, size * 0.26, size * 0.74, size / 2
            d.rectangle([cx - gap - bw, top, cx - gap, bot], fill=fg_hex)
            d.rectangle([cx + gap, top, cx + gap + bw, bot], fill=fg_hex)
        elif kind == "stop":
            m = size * 0.30
            try:
                d.rounded_rectangle([m, m, size - m, size - m], radius=size * 0.08, fill=fg_hex)
            except AttributeError:
                d.rectangle([m, m, size - m, size - m], fill=fg_hex)
        elif kind == "next":
            bw = size * 0.09
            d.polygon([(size * 0.26, size * 0.24), (size * 0.26, size * 0.76), (size * 0.60, size * 0.5)], fill=fg_hex)
            d.rectangle([size * 0.64, size * 0.24, size * 0.64 + bw, size * 0.76], fill=fg_hex)
        elif kind == "prev":
            bw = size * 0.09
            d.polygon([(size * 0.74, size * 0.24), (size * 0.74, size * 0.76), (size * 0.40, size * 0.5)], fill=fg_hex)
            d.rectangle([size * 0.36 - bw, size * 0.24, size * 0.36, size * 0.76], fill=fg_hex)
        elif kind == "shuffle":
            d.line([(size * 0.16, size * 0.68), (size * 0.60, size * 0.30)], fill=fg_hex, width=w)
            d.polygon([(size * 0.60, size * 0.16), (size * 0.84, size * 0.30), (size * 0.58, size * 0.42)], fill=fg_hex)
            d.line([(size * 0.16, size * 0.30), (size * 0.60, size * 0.68)], fill=fg_hex, width=w)
            d.polygon([(size * 0.60, size * 0.56), (size * 0.84, size * 0.70), (size * 0.58, size * 0.82)], fill=fg_hex)
        elif kind == "repeat":
            cx, cy = size / 2, size / 2
            r = size * 0.30
            ah = size * 0.16  # longueur de la pointe de flèche
            # Arc supérieur (gauche -> haut -> droite) ; la flèche part exactement de son extrémité réelle
            end1 = 340
            a1 = math.radians(end1)
            d.arc([cx - r, cy - r, cx + r, cy + r], start=200, end=end1, fill=fg_hex, width=w)
            self._arrowhead(d, (cx + r * math.cos(a1), cy + r * math.sin(a1)), a1 + math.pi / 2, ah, fg_hex)
            # Arc inférieur (droite -> bas -> gauche), symétrique
            end2 = 160
            a2 = math.radians(end2)
            d.arc([cx - r, cy - r, cx + r, cy + r], start=20, end=end2, fill=fg_hex, width=w)
            self._arrowhead(d, (cx + r * math.cos(a2), cy + r * math.sin(a2)), a2 + math.pi / 2, ah, fg_hex)
        elif kind == "volume":
            # Haut-parleur (trapèze + arcs d'onde sonore)
            d.polygon([(size * 0.22, size * 0.40), (size * 0.36, size * 0.40), (size * 0.52, size * 0.26),
                       (size * 0.52, size * 0.74), (size * 0.36, size * 0.60), (size * 0.22, size * 0.60)], fill=fg_hex)
            d.arc([size * 0.56, size * 0.32, size * 0.72, size * 0.68], start=300, end=60, fill=fg_hex, width=w)
            d.arc([size * 0.56, size * 0.20, size * 0.84, size * 0.80], start=300, end=60, fill=fg_hex, width=w)
        elif kind == "volume_muted":
            d.polygon([(size * 0.22, size * 0.40), (size * 0.36, size * 0.40), (size * 0.52, size * 0.26),
                       (size * 0.52, size * 0.74), (size * 0.36, size * 0.60), (size * 0.22, size * 0.60)], fill=fg_hex)
            d.line([(size * 0.60, size * 0.36), (size * 0.80, size * 0.64)], fill=fg_hex, width=w)
            d.line([(size * 0.80, size * 0.36), (size * 0.60, size * 0.64)], fill=fg_hex, width=w)
        elif kind == "add":
            bw, cx, cy, L = size * 0.11, size / 2, size / 2, size * 0.28
            d.rectangle([cx - bw / 2, cy - L, cx + bw / 2, cy + L], fill=fg_hex)
            d.rectangle([cx - L, cy - bw / 2, cx + L, cy + bw / 2], fill=fg_hex)
        elif kind == "delete":
            cx, cy, L = size / 2, size / 2, size * 0.22
            d.line([(cx - L, cy - L), (cx + L, cy + L)], fill=fg_hex, width=w)
            d.line([(cx - L, cy + L), (cx + L, cy - L)], fill=fg_hex, width=w)
        elif kind == "home":
            d.polygon([(size * 0.5, size * 0.16), (size * 0.86, size * 0.46), (size * 0.72, size * 0.46),
                       (size * 0.72, size * 0.84), (size * 0.28, size * 0.84), (size * 0.28, size * 0.46),
                       (size * 0.14, size * 0.46)], fill=fg_hex)
        elif kind == "disc":
            d.ellipse([size * 0.14, size * 0.14, size * 0.86, size * 0.86], outline=fg_hex, width=w)
            r = size * 0.12
            d.ellipse([size / 2 - r, size / 2 - r, size / 2 + r, size / 2 + r], fill=fg_hex)
        elif kind == "mic":
            try:
                d.rounded_rectangle([size * 0.38, size * 0.10, size * 0.62, size * 0.56], radius=size * 0.12, fill=fg_hex)
            except AttributeError:
                d.rectangle([size * 0.38, size * 0.10, size * 0.62, size * 0.56], fill=fg_hex)
            d.arc([size * 0.24, size * 0.30, size * 0.76, size * 0.72], start=0, end=180, fill=fg_hex, width=w)
            d.line([(size * 0.5, size * 0.72), (size * 0.5, size * 0.86)], fill=fg_hex, width=w)
            d.line([(size * 0.32, size * 0.86), (size * 0.68, size * 0.86)], fill=fg_hex, width=w)
        elif kind == "queue":
            bar_h = size * 0.10
            for i, ypos in enumerate((0.26, 0.5, 0.74)):
                bw = size * (0.68 - i * 0.14)
                try:
                    d.rounded_rectangle([size * 0.16, size * ypos - bar_h / 2, size * 0.16 + bw, size * ypos + bar_h / 2], radius=bar_h / 2, fill=fg_hex)
                except AttributeError:
                    d.rectangle([size * 0.16, size * ypos - bar_h / 2, size * 0.16 + bw, size * ypos + bar_h / 2], fill=fg_hex)
        elif kind == "note":
            d.ellipse([size * 0.16, size * 0.62, size * 0.46, size * 0.88], fill=fg_hex)
            d.rectangle([size * 0.42, size * 0.14, size * 0.50, size * 0.76], fill=fg_hex)
            d.polygon([(size * 0.50, size * 0.14), (size * 0.84, size * 0.26), (size * 0.84, size * 0.40), (size * 0.50, size * 0.28)], fill=fg_hex)
        elif kind == "edit":
            d.polygon([(size * 0.22, size * 0.86), (size * 0.18, size * 0.68), (size * 0.60, size * 0.26),
                       (size * 0.76, size * 0.42), (size * 0.34, size * 0.84)], fill=fg_hex)
            d.polygon([(size * 0.60, size * 0.26), (size * 0.70, size * 0.16), (size * 0.86, size * 0.32), (size * 0.76, size * 0.42)], fill=fg_hex)
        elif kind == "folder":
            d.polygon([(size * 0.14, size * 0.30), (size * 0.42, size * 0.30), (size * 0.50, size * 0.40),
                       (size * 0.86, size * 0.40), (size * 0.86, size * 0.78), (size * 0.14, size * 0.78)], fill=fg_hex)
        elif kind == "save":
            d.polygon([(size * 0.36, size * 0.18), (size * 0.64, size * 0.18), (size * 0.64, size * 0.48),
                       (size * 0.80, size * 0.48), (size * 0.50, size * 0.76), (size * 0.20, size * 0.48),
                       (size * 0.36, size * 0.48)], fill=fg_hex)
            d.rectangle([size * 0.20, size * 0.82, size * 0.80, size * 0.88], fill=fg_hex)
        elif kind == "undo":
            cx, cy, r = size * 0.52, size * 0.56, size * 0.26
            start = 30
            a1 = math.radians(start)
            d.arc([cx - r, cy - r, cx + r, cy + r], start=start, end=300, fill=fg_hex, width=w)
            tip = (cx + r * math.cos(a1), cy + r * math.sin(a1))
            self._arrowhead(d, tip, a1 - math.pi / 2, size * 0.16, fg_hex)
        elif kind == "picture":
            try:
                d.rounded_rectangle([size * 0.14, size * 0.20, size * 0.86, size * 0.80], radius=size * 0.06, outline=fg_hex, width=w)
            except AttributeError:
                d.rectangle([size * 0.14, size * 0.20, size * 0.86, size * 0.80], outline=fg_hex, width=w)
            d.ellipse([size * 0.24, size * 0.30, size * 0.36, size * 0.42], fill=fg_hex)
            d.polygon([(size * 0.20, size * 0.74), (size * 0.42, size * 0.48), (size * 0.60, size * 0.64),
                       (size * 0.72, size * 0.50), (size * 0.80, size * 0.74)], fill=fg_hex)
        elif kind == "bolt":
            d.polygon([(size * 0.56, size * 0.14), (size * 0.28, size * 0.56), (size * 0.46, size * 0.56),
                       (size * 0.40, size * 0.86), (size * 0.74, size * 0.42), (size * 0.54, size * 0.42)], fill=fg_hex)

        img = img.resize((diameter, diameter), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(img, master=self.root)

    def _build_playback_icons(self):
        """Pré-génère (une seule fois) toutes les icônes rondes de la barre de lecture,
        en version normale et survol, pour un échange instantané sans recalcul à chaque clic."""
        SZ_MAIN, SZ_SIDE, SZ_STOP, SZ_MODE = 60, 46, 42, 38
        C = {"side": "#64748b", "stop": "#ef4444", "play": "#10b981", "pause": "#f59e0b", "mode_on": "#3b82f6"}

        self.icons = {
            "prev": self._make_icon("prev", SZ_SIDE, C["side"]),
            "prev_hover": self._make_icon("prev", SZ_SIDE, self._lighten(C["side"])),
            "next": self._make_icon("next", SZ_SIDE, C["side"]),
            "next_hover": self._make_icon("next", SZ_SIDE, self._lighten(C["side"])),
            "stop": self._make_icon("stop", SZ_STOP, C["stop"]),
            "stop_hover": self._make_icon("stop", SZ_STOP, self._lighten(C["stop"])),
            "play": self._make_icon("play", SZ_MAIN, C["play"]),
            "play_hover": self._make_icon("play", SZ_MAIN, self._lighten(C["play"])),
            "pause": self._make_icon("pause", SZ_MAIN, C["pause"]),
            "pause_hover": self._make_icon("pause", SZ_MAIN, self._lighten(C["pause"])),
            "shuffle_off": self._make_icon("shuffle", SZ_MODE, C["side"]),
            "shuffle_off_hover": self._make_icon("shuffle", SZ_MODE, self._lighten(C["side"])),
            "shuffle_on": self._make_icon("shuffle", SZ_MODE, C["mode_on"]),
            "shuffle_on_hover": self._make_icon("shuffle", SZ_MODE, self._lighten(C["mode_on"])),
            "repeat_off": self._make_icon("repeat", SZ_MODE, C["side"]),
            "repeat_off_hover": self._make_icon("repeat", SZ_MODE, self._lighten(C["side"])),
            "repeat_on": self._make_icon("repeat", SZ_MODE, C["mode_on"]),
            "repeat_on_hover": self._make_icon("repeat", SZ_MODE, self._lighten(C["mode_on"])),
            "volume": self._make_icon("volume", 22, "#f1f5f9", C["side"]),
            "volume_muted": self._make_icon("volume_muted", 22, "#f1f5f9", "#ef4444"),
            "pl_add": self._make_icon("add", 30, "#10b981"),
            "pl_add_hover": self._make_icon("add", 30, self._lighten("#10b981")),
            "pl_delete": self._make_icon("delete", 30, "#ef4444"),
            "pl_delete_hover": self._make_icon("delete", 30, self._lighten("#ef4444")),
            "nav_queue": self._make_icon("queue", 20, None, "#ffffff"),
            "nav_home": self._make_icon("home", 20, None, "#cbd5e1"),
            "nav_disc": self._make_icon("disc", 20, None, "#cbd5e1"),
            "nav_mic": self._make_icon("mic", 20, None, "#cbd5e1"),
            "app_note": self._make_icon("note", 18, None, "#94a3b8"),
            "app_edit": self._make_icon("edit", 18, None, "#94a3b8"),
            "ed_folder": self._make_icon("folder", 18, None, "#ffffff"),
            "ed_bolt": self._make_icon("bolt", 18, None, "#ffffff"),
            "ed_undo": self._make_icon("undo", 18, None, "#ffffff"),
            "ed_undo_off": self._make_icon("undo", 18, None, "#cbd5e1"),
            "ed_picture": self._make_icon("picture", 26, None, "#64748b"),
            "ed_delete": self._make_icon("delete", 26, None, "#ef4444"),
            "ed_save": self._make_icon("save", 18, None, "#ffffff"),
        }

    def set_placeholder_cover(self):
        img = Image.new('RGB', (220, 220), color='#1e293b'); draw = ImageDraw.Draw(img)
        draw.ellipse([20, 20, 200, 200], fill='#334155', outline='#475569', width=3)
        draw.ellipse([80, 80, 140, 140], fill='#3b82f6')
        photo = ImageTk.PhotoImage(img, master=self.root)
        self.cover_label.config(image=photo); self.current_cover = photo

    def update_cover_art(self, track_path):
        try:
            tag = TinyTag.get(track_path, image=True)
            if tag.get_image():
                img = Image.open(io.BytesIO(tag.get_image())).resize((220, 220), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(img, master=self.root)
                self.cover_label.config(image=photo); self.current_cover = photo
                return
        except Exception as e:
            logger.debug("Pas de pochette embarquée pour %s (%s), pochette par défaut utilisée.", track_path, e)
        self.set_placeholder_cover()

    def format_time(self, seconds):
        mins, secs = divmod(int(seconds), 60); return f"{mins:02d}:{secs:02d}"

    def update_timer(self):
        if self.audio.is_busy() and not self.paused and not self.is_sliding:
            current_time = self.audio.get_position()
            if current_time >= 0:
                if current_time > self.track_length > 0: current_time = self.track_length
                self.lbl_time_current.config(text=self.format_time(current_time))
                self.progress_set(current_time)
        self.root.after(500, self.update_timer)

    def on_slide_press(self, event):
        self.is_sliding = True
        self.audio.begin_seek()

    def on_slide_release(self, event):
        if self.is_stopped or self.track_length == 0 or not self.current_track_path:
            self.progress_set(0); self.is_sliding = False; self.audio.end_seek(); return
        try:
            self.audio.play(start=self.progress_get())
            self.paused = False; self.update_play_pause_button()
        except Exception as e:
            logger.warning("Échec du déplacement dans la piste %s : %s", self.current_track_path, e)
        self.is_sliding = False
        self.audio.end_seek()

    def toggle_play_pause(self):
        """Bouton unique Lecture/Pause : lance la lecture si rien n'est en cours, sinon bascule pause/reprise."""
        if self.is_stopped:
            # Rien n'est chargé : on démarre la lecture selon le contexte (comportement de l'ancien bouton "Lecture")
            self.btn_play_clicked()
        elif self.paused:
            self.audio.unpause()
            self.paused = False
            self.update_play_pause_button()
        else:
            self.audio.pause()
            self.paused = True
            self.update_play_pause_button()

    def update_play_pause_button(self):
        """Met à jour l'icône du bouton unique selon l'état de lecture actuel."""
        if self.is_stopped or self.paused:
            self.btn_play_pause.config(image=self.icons["play"])
        else:
            self.btn_play_pause.config(image=self.icons["pause"])

    def stop(self):
        self.audio.stop(); self.paused = False; self.is_stopped = True
        self.youtube_source_active = False  # un arrêt explicite sort du mode radio YouTube
        self.update_play_pause_button()
        self.current_track_path = None; self.lbl_now_playing_title.config(text="Aucun morceau")
        self.lbl_now_playing_album.config(text=""); self.progress_set(0); self.lbl_time_current.config(text="00:00")
        self.lbl_time_total.config(text="00:00"); self.set_placeholder_cover()
        if self.current_view == "queue": self.refresh_queue_tree()

class MetadataEditor:
    """Éditeur de métadonnées audio, adapté de 'Editeur méta musique.py' pour s'intégrer dans
    la même fenêtre que le Lecteur (voir MusicPlayer.show_app_editeur). Construit son interface
    dans `parent` (un Frame fourni par MusicPlayer) au lieu de `root` directement -- `root` reste
    utilisé pour les Toplevel/after/positionnement, qui doivent rester liés à la vraie fenêtre.
    Logique métier (backup/undo, tagging MusicBrainz, conversion ffmpeg...) inchangée."""

    def __init__(self, parent, root, font_family, icons):
        self.parent = parent
        self.root = root
        self.font_family = font_family
        self.icons = icons

        self.dossier_actuel = ""
        self.nouvelle_pochette_data = None
        self.nouvelle_pochette_mime = None
        self.photo_img = None
        self._drag_anchor = None
        self._sort_col = None
        self._sort_asc = True

        self.api_cache = {}
        self._historique = []

        self.cles_tags = [
            "tracknumber", "title", "artist", "albumartist", "album",
            "date", "genre", "composer", "discnumber", "isrc",
            "organization", "originaldate", "compilation"
        ]

        self.creer_interface()

    def creer_interface(self):
        frame_haut = tk.Frame(self.parent, bg="#f8fafc", pady=14, padx=20)
        frame_haut.pack(fill=tk.X)

        tk.Button(
            frame_haut, text=" Choisir un dossier", image=self.icons["ed_folder"], compound=tk.LEFT,
            font=(self.font_family, 10, "bold"), bg="#3b82f6", fg="white", relief=tk.FLAT,
            cursor="hand2", padx=10, pady=6, command=self.choisir_dossier
        ).pack(side=tk.LEFT)

        self.lbl_dossier = tk.Label(frame_haut, text="Aucun dossier sélectionné", bg="#f8fafc", fg="#64748b", font=(self.font_family, 9))
        self.lbl_dossier.pack(side=tk.LEFT, padx=12)

        tk.Button(
            frame_haut, text=" Mode Auto (MusicBrainz)", image=self.icons["ed_bolt"], compound=tk.LEFT,
            command=self.lancer_mode_auto, bg="#f59e0b", fg="white", font=(self.font_family, 9, "bold"),
            relief=tk.FLAT, cursor="hand2", padx=8, pady=6
        ).pack(side=tk.LEFT, padx=(0, 6))

        self.btn_undo = tk.Button(
            frame_haut, text=" Annuler (0)", image=self.icons["ed_undo_off"], compound=tk.LEFT,
            command=self.annuler_derniere_action, bg="#e2e8f0", fg="#94a3b8", font=(self.font_family, 9, "bold"),
            relief=tk.FLAT, cursor="hand2", padx=8, pady=6, state=tk.DISABLED
        )
        self.btn_undo.pack(side=tk.LEFT, padx=(10, 0))

        self.progress_global = ttk.Progressbar(frame_haut, orient=tk.HORIZONTAL, length=200, mode='indeterminate')

        frame_milieu = tk.Frame(self.parent, bg="#f8fafc", padx=20)
        frame_milieu.pack(fill=tk.BOTH, expand=True)

        self.colonnes = ("fichier", "piste", "titre", "artiste", "albumartist", "album",
                         "date", "genre", "compositeur", "disque", "isrc", "label", "date_orig", "compil", "chemin")

        self.tree = ttk.Treeview(frame_milieu, columns=self.colonnes, show="headings", selectmode="extended")

        config_col = {
            "fichier":     ("Fichier", 140, True),
            "piste":       ("Piste", 45, False),
            "titre":       ("Titre", 110, True),
            "artiste":     ("Artiste", 110, True),
            "albumartist": ("Art. Album", 100, True),
            "album":       ("Album", 110, True),
            "date":        ("Année", 50, False),
            "genre":       ("Genre", 80, True),
            "compositeur": ("Compositeur", 90, True),
            "disque":      ("Disque", 50, False),
            "isrc":        ("ISRC", 80, False),
            "label":       ("Label", 90, True),
            "date_orig":   ("Date Orig.", 65, False),
            "compil":      ("Compil.", 55, False),
            "chemin":      ("", 0, False),
        }

        for col, (label, w, stretch) in config_col.items():
            self.tree.heading(col, text=label, command=lambda c=col: self._trier_par(c))
            self.tree.column(col, width=w, stretch=stretch)

        self.tree.tag_configure("invalide", foreground="#ef4444")
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)

        sb = ttk.Scrollbar(frame_milieu, orient=tk.VERTICAL, command=self.tree.yview, style="Modern.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<<TreeviewSelect>>", self.afficher_selection)
        self.tree.bind("<Control-a>",        self._selectionner_tout)
        self.tree.bind("<ButtonPress-1>",    self._drag_debut)
        self.tree.bind("<B1-Motion>",        self._drag_mouvement)

        frame_bas = tk.LabelFrame(self.parent, text="Éditer la sélection", padx=14, pady=10,
                                   bg="#f8fafc", fg="#1e293b", font=(self.font_family, 10, "bold"))
        frame_bas.pack(fill=tk.X, padx=20, pady=(0, 16))

        frame_poch = tk.Frame(frame_bas, bg="#f8fafc")
        frame_poch.grid(row=0, column=0, rowspan=4, padx=(0, 16), pady=2, sticky=tk.N)

        self.lbl_pochette = tk.Label(frame_poch, text="Pas de\npochette", width=11, height=5, relief="flat",
                                      bg="#e2e8f0", fg="#94a3b8", font=(self.font_family, 9))
        self.lbl_pochette.pack()
        tk.Button(frame_poch, image=self.icons["ed_picture"], bg="#f8fafc", activebackground="#f8fafc",
                  relief=tk.FLAT, bd=0, cursor="hand2", command=self.choose_cover).pack(pady=(6, 2))
        tk.Button(frame_poch, image=self.icons["ed_delete"], bg="#f8fafc", activebackground="#f8fafc",
                  relief=tk.FLAT, bd=0, cursor="hand2", command=self.delete_cover).pack()

        entry_style = {"font": (self.font_family, 9), "relief": tk.FLAT, "highlightthickness": 1,
                       "highlightbackground": "#e2e8f0", "highlightcolor": "#3b82f6", "bg": "white"}

        self.entry_piste        = tk.Entry(frame_bas, width=8, **entry_style)
        self.entry_titre        = tk.Entry(frame_bas, width=22, **entry_style)
        self.entry_artiste      = tk.Entry(frame_bas, width=22, **entry_style)
        self.entry_albumartist  = tk.Entry(frame_bas, width=22, **entry_style)
        self.entry_album        = tk.Entry(frame_bas, width=22, **entry_style)
        self.entry_date         = tk.Entry(frame_bas, width=8, **entry_style)
        self.entry_genre        = tk.Entry(frame_bas, width=18, **entry_style)
        self.entry_compositeur  = tk.Entry(frame_bas, width=22, **entry_style)
        self.entry_disque       = tk.Entry(frame_bas, width=8, **entry_style)
        self.entry_isrc         = tk.Entry(frame_bas, width=18, **entry_style)
        self.entry_label        = tk.Entry(frame_bas, width=22, **entry_style)
        self.entry_date_orig    = tk.Entry(frame_bas, width=8, **entry_style)
        self.entry_compil       = tk.Entry(frame_bas, width=8, **entry_style)

        champs = [
            (0, 1, "Piste # :", self.entry_piste),
            (0, 3, "Titre :", self.entry_titre),
            (0, 5, "Artiste :", self.entry_artiste),
            (0, 7, "Artiste Album :", self.entry_albumartist),
            (1, 1, "Album :", self.entry_album),
            (1, 3, "Année :", self.entry_date),
            (1, 5, "Genre :", self.entry_genre),
            (1, 7, "Compositeur :", self.entry_compositeur),
            (2, 1, "Disque # :", self.entry_disque),
            (2, 3, "Code ISRC :", self.entry_isrc),
            (2, 5, "Label :", self.entry_label),
            (2, 7, "Date Originale :", self.entry_date_orig),
            (3, 1, "Compilation (0/1) :", self.entry_compil)
        ]

        for row, col, label, entry in champs:
            tk.Label(frame_bas, text=label, bg="#f8fafc", fg="#475569", font=(self.font_family, 9)).grid(row=row, column=col, sticky=tk.E, padx=(6, 2), pady=3)
            entry.grid(row=row, column=col + 1, padx=(0, 6), pady=3, sticky=tk.W)
            entry.bind("<Return>", lambda e: self.sauvegarder_metadonnees())

        self.lbl_info = tk.Label(frame_bas, text="", fg="#3b82f6", bg="#f8fafc", font=(self.font_family, 9, "italic"))
        self.lbl_info.grid(row=3, column=3, columnspan=4, sticky=tk.W, padx=6)

        tk.Button(
            frame_bas, text=" Sauvegarder", image=self.icons["ed_save"], compound=tk.LEFT,
            command=self.sauvegarder_metadonnees, bg="#10b981", fg="white",
            font=(self.font_family, 11, "bold"), relief=tk.FLAT, cursor="hand2", padx=12, pady=6
        ).grid(row=3, column=7, columnspan=2, sticky=tk.EW, padx=(6, 0), pady=4)

    # =========================================================
    # SYSTEME D'HISTORIQUE (UNDO / BACKUP)
    # =========================================================

    def _creer_backup(self, chemin):
        """Prend une capture en mémoire de tous les tags d'un fichier avant modification."""
        try:
            try:
                audio = EasyID3(chemin)
            except Exception:
                audio = {}
            tags = {cle: audio.get(cle, []) for cle in self.cles_tags}
            cover_data, cover_mime = self._lire_pochette(chemin)
            return {"tags": tags, "cover_data": cover_data, "cover_mime": cover_mime}
        except Exception:
            return None

    def _restaurer_backup(self, chemin, backup_data):
        """Réinjecte l'ancienne sauvegarde sur le fichier actuel."""
        if not backup_data or not os.path.exists(chemin):
            return
        try:
            t_bruts = ID3(chemin)
            for tg in ["TCOP", "WCOP", "TENC", "TSSE", "USER"]:
                t_bruts.delall(tg)
            t_bruts.delall("APIC")
            if backup_data["cover_data"]:
                t_bruts.add(APIC(encoding=3, mime=backup_data["cover_mime"], type=3, desc="Cover", data=backup_data["cover_data"]))
            t_bruts.save(chemin)

            audio = EasyID3(chemin)
            for cle_pure in ["copyright", "license", "encodedby"]:
                if cle_pure in audio:
                    del audio[cle_pure]

            for cle, val in backup_data["tags"].items():
                if val:
                    audio[cle] = val
                elif cle in audio:
                    del audio[cle]
            audio.save()
        except Exception:
            pass

    def _ajouter_historique(self, backup_batch):
        """Ajoute le lot de backups à l'historique et active le bouton Annuler."""
        if backup_batch:
            self._historique.append(backup_batch)
            self.root.after(0, lambda: self.btn_undo.config(
                state=tk.NORMAL, text=f" Annuler ({len(self._historique)})",
                image=self.icons["ed_undo"], bg="#64748b", fg="white"
            ))

    def annuler_derniere_action(self):
        """Dépile le dernier lot de modifications et restaure les fichiers (Ctrl+Z)."""
        if not self._historique:
            return
        batch = self._historique.pop()

        if self._historique:
            self.btn_undo.config(text=f" Annuler ({len(self._historique)})")
        else:
            self.btn_undo.config(state=tk.DISABLED, text=" Annuler (0)", image=self.icons["ed_undo_off"], bg="#e2e8f0", fg="#94a3b8")

        self.progress_global.pack(side=tk.LEFT, padx=10)
        self.progress_global.start(10)

        def thread_restauration():
            for chemin, backup_data in batch.items():
                self._restaurer_backup(chemin, backup_data)

            self.root.after(0, lambda: [
                self.progress_global.stop(),
                self.progress_global.pack_forget(),
                self.charger_fichiers(),
                messagebox.showinfo("Restauration", "Les anciens tags ont été restaurés avec succès.")
            ])

        threading.Thread(target=thread_restauration, daemon=True).start()

    # =========================================================
    # MÉTHODES DE NAVIGATION ET TREEVIEW
    # =========================================================

    def _selectionner_tout(self, event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _trier_par(self, col):
        col_idx = self.colonnes.index(col)
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True

        items = [(self.tree.set(iid, col), iid) for iid in self.tree.get_children()]

        def cle(t):
            v = t[0]
            if col in ("piste", "date", "disque", "date_orig"):
                try:
                    return (0, int(v.split('/')[0]))
                except ValueError:
                    return (1, v.lower())
            return v.lower()

        items.sort(key=cle, reverse=not self._sort_asc)
        for index, (_, iid) in enumerate(items):
            self.tree.move(iid, "", index)

    def choisir_dossier(self):
        dossier_defaut = str(Path.home() / "Music")
        if not os.path.exists(dossier_defaut):
            dossier_defaut = None

        dossier = filedialog.askdirectory(initialdir=dossier_defaut)
        if dossier:
            self.dossier_actuel = dossier
            self.lbl_dossier.config(text=dossier, fg="#1e293b")
            self.charger_fichiers()

    def charger_fichiers(self):
        for row in self.tree.get_children():
            self.tree.delete(row)

        try:
            noms = sorted(f for f in os.listdir(self.dossier_actuel) if f.lower().endswith(FORMATS_SUPPORTES_EDITEUR))
        except OSError as e:
            messagebox.showerror("Erreur", f"Impossible de lire le dossier :\n{e}")
            return

        for nom in noms:
            chemin = os.path.join(self.dossier_actuel, nom)
            self._ajouter_ligne(nom, chemin)

    def _tag(self, audio, cle):
        try:
            if audio and cle in audio:
                return ", ".join(audio[cle])
        except Exception:
            pass
        return ""

    def _ajouter_ligne(self, nom, chemin):
        est_invalide = detecter_faux_mp3(chemin)
        try:
            audio = EasyID3(chemin) if not est_invalide else None
        except Exception:
            audio = None

        if est_invalide:
            self.tree.insert("", tk.END, tags=("invalide",), values=(
                f"⚠ {nom}", "", "", "", "", "", "", "", "", "", "", "", "", "", chemin))
        else:
            self.tree.insert("", tk.END, values=(
                nom,
                self._tag(audio, "tracknumber"),
                self._tag(audio, "title"),
                self._tag(audio, "artist"),
                self._tag(audio, "albumartist"),
                self._tag(audio, "album"),
                self._tag(audio, "date"),
                self._tag(audio, "genre"),
                self._tag(audio, "composer"),
                self._tag(audio, "discnumber"),
                self._tag(audio, "isrc"),
                self._tag(audio, "organization"),
                self._tag(audio, "originaldate"),
                self._tag(audio, "compilation"),
                chemin,
            ))

    def _mettre_a_jour_ligne(self, item_id, chemin):
        nom = os.path.basename(chemin)
        est_invalide = detecter_faux_mp3(chemin)
        try:
            audio = EasyID3(chemin) if not est_invalide else None
        except Exception:
            audio = None

        if est_invalide:
            self.tree.item(item_id, tags=("invalide",), values=(
                f"⚠ {nom}", "", "", "", "", "", "", "", "", "", "", "", "", "", chemin))
        else:
            self.tree.item(item_id, tags=(), values=(
                nom,
                self._tag(audio, "tracknumber"),
                self._tag(audio, "title"),
                self._tag(audio, "artist"),
                self._tag(audio, "albumartist"),
                self._tag(audio, "album"),
                self._tag(audio, "date"),
                self._tag(audio, "genre"),
                self._tag(audio, "composer"),
                self._tag(audio, "discnumber"),
                self._tag(audio, "isrc"),
                self._tag(audio, "organization"),
                self._tag(audio, "originaldate"),
                self._tag(audio, "compilation"),
                chemin,
            ))

    def afficher_selection(self, event=None):
        self.nouvelle_pochette_data = None
        self.nouvelle_pochette_mime = None

        selection = self.tree.selection()
        n = len(selection)

        if n == 0:
            self.lbl_info.config(text="")
            return

        if n == 1:
            self.lbl_info.config(text="1 fichier sélectionné")
            v = self.tree.item(selection[0])["values"]
            self._set(self.entry_piste,       v[1])
            self._set(self.entry_titre,       v[2])
            self._set(self.entry_artiste,     v[3])
            self._set(self.entry_albumartist, v[4])
            self._set(self.entry_album,       v[5])
            self._set(self.entry_date,        v[6])
            self._set(self.entry_genre,       v[7])
            self._set(self.entry_compositeur, v[8])
            self._set(self.entry_disque,      v[9])
            self._set(self.entry_isrc,        v[10])
            self._set(self.entry_label,       v[11])
            self._set(self.entry_date_orig,   v[12])
            self._set(self.entry_compil,      v[13])
            self._afficher_pochette(str(v[14]))
        else:
            self.lbl_info.config(text=f"{n} fichiers sélectionnés — Les champs vides seront ignorés.")
            all_v = [self.tree.item(s)["values"] for s in selection]

            champs_multi = [
                (self.entry_piste, 1),       (self.entry_titre, 2),
                (self.entry_artiste, 3),     (self.entry_albumartist, 4),
                (self.entry_album, 5),       (self.entry_date, 6),
                (self.entry_genre, 7),       (self.entry_compositeur, 8),
                (self.entry_disque, 9),      (self.entry_isrc, 10),
                (self.entry_label, 11),      (self.entry_date_orig, 12),
                (self.entry_compil, 13)
            ]
            for entry, idx in champs_multi:
                vals = [str(v[idx]) for v in all_v]
                self._set(entry, vals[0] if len(set(vals)) == 1 else "")

            self.photo_img = None
            self.lbl_pochette.config(image="", text=f"{n} fichiers", width=11, height=5, fg="#94a3b8")

    def _set(self, entry, value):
        entry.delete(0, tk.END)
        if value not in (None, "", "None"):
            entry.insert(0, str(value))

    def sauvegarder_metadonnees(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning("Attention", "Veuillez sélectionner au moins un fichier.")
            return

        self.progress_global.pack(side=tk.LEFT, padx=10)
        self.progress_global.start(10)

        selection_multiple = len(selection) > 1

        def thread_sauvegarde():
            erreurs = []
            mises_a_jour = []
            backup_batch = {}
            for item_id in selection:
                valeurs = self.tree.item(item_id)["values"]
                chemin_initial = str(valeurs[14])
                nom_fichier = str(valeurs[0])

                if nom_fichier.startswith("⚠") or detecter_faux_mp3(chemin_initial):
                    chemin_final, err = convertir_en_mp3(chemin_initial, callback_progression=None)
                    if not chemin_final:
                        erreurs.append(f"• {nom_fichier} (Conversion échouée) : {err}")
                        continue
                    chemin_actuel = chemin_final
                else:
                    chemin_actuel = chemin_initial

                backup_data = self._creer_backup(chemin_actuel)
                if backup_data:
                    backup_batch[chemin_actuel] = backup_data

                try:
                    self._sauvegarder_item(item_id, chemin_actuel, selection_multiple)
                    mises_a_jour.append((item_id, chemin_actuel))
                except Exception as e:
                    erreurs.append(f"• {nom_fichier} : {e}")

            self._ajouter_historique(backup_batch)

            def finaliser_ihm():
                self.progress_global.stop()
                self.progress_global.pack_forget()

                for item_id, ch in mises_a_jour:
                    self._mettre_a_jour_ligne(item_id, ch)

                if erreurs:
                    messagebox.showerror("Erreurs lors du traitement", "\n".join(erreurs))
                else:
                    messagebox.showinfo("Succès", "Sauvegarde effectuée avec succès !")

            self.root.after(0, finaliser_ihm)

        threading.Thread(target=thread_sauvegarde, daemon=True).start()

    def _sauvegarder_item(self, item_id, chemin, selection_multiple=False):
        try:
            tags_bruts = ID3(chemin)
        except ID3NoHeaderError:
            tags_bruts = ID3()
            tags_bruts.save(chemin)
            tags_bruts = ID3(chemin)

        for tag_copy in ["TCOP", "WCOP", "TENC", "TSSE", "USER"]:
            tags_bruts.delall(tag_copy)
        tags_bruts.save(chemin)

        audio = EasyID3(chemin)
        for cle_pure in ["copyright", "license", "encodedby"]:
            if cle_pure in audio:
                del audio[cle_pure]

        mappage_champs = [
            ("tracknumber", self.entry_piste),   ("title",       self.entry_titre),
            ("artist",      self.entry_artiste), ("albumartist", self.entry_albumartist),
            ("album",       self.entry_album),   ("date",        self.entry_date),
            ("genre",       self.entry_genre),   ("composer",    self.entry_compositeur),
            ("discnumber",  self.entry_disque),  ("isrc",        self.entry_isrc),
            ("organization", self.entry_label),  ("originaldate", self.entry_date_orig),
            ("compilation", self.entry_compil),
        ]

        for cle_tag, widget in mappage_champs:
            valeur = widget.get().strip()
            if valeur:
                audio[cle_tag] = [valeur]
            else:
                if selection_multiple:
                    continue
                if cle_tag in audio:
                    del audio[cle_tag]
        audio.save()

        if self.nouvelle_pochette_data is not None:
            tags_id3 = ID3(chemin)
            tags_id3.delall("APIC")
            if len(self.nouvelle_pochette_data) > 0:
                tags_id3.add(APIC(
                    encoding=3, mime=self.nouvelle_pochette_mime,
                    type=3, desc="Cover", data=self.nouvelle_pochette_data
                ))
            tags_id3.save(chemin)

    # =========================================================
    # AIGUILLAGE ET ROUTING INTELLIGENT (MODE AUTO)
    # =========================================================

    def lancer_mode_auto(self):
        """Détecte l'agencement du dossier pour lancer le bon algorithme."""
        if not self.dossier_actuel:
            messagebox.showwarning("Aucun dossier", "Veuillez d'abord choisir un dossier.")
            return

        nom_dossier = os.path.basename(os.path.normpath(self.dossier_actuel)).strip().upper()

        if nom_dossier in ["DIVERS", "NA", "A COLORS SHOW"]:
            print(nom_dossier)
            self._lancer_interface_vrac(nom_dossier)
        else:
            print(nom_dossier)
            self._lancer_interface_album()

    # =========================================================
    # BRANCHE A : MODE ALBUM STRUCTURÉ
    # =========================================================

    def _lancer_interface_album(self):
        if not ffmpeg_disponible():
            messagebox.showerror("ffmpeg introuvable", "Veuillez installer ffmpeg pour utiliser le mode auto.")
            return

        clean_path = os.path.normpath(self.dossier_actuel)
        album_defaut = os.path.basename(clean_path)
        artiste_defaut = os.path.basename(os.path.dirname(clean_path))

        pop = tk.Toplevel(self.root)
        pop.title("Configuration Mode Auto")
        pop.geometry("420x220")
        pop.resizable(False, False)
        pop.configure(bg="#f8fafc")
        pop.transient(self.root)
        pop.grab_set()
        pop.geometry(f"+{self.root.winfo_x() + 150}+{self.root.winfo_y() + 150}")

        tk.Label(pop, text="Ajustez les termes de recherche si nécessaire :", font=(self.font_family, 10, "bold"), bg="#f8fafc", fg="#1e293b").pack(pady=14)
        frame_form = tk.Frame(pop, bg="#f8fafc")
        frame_form.pack(fill=tk.X, padx=25)

        entry_style = {"font": (self.font_family, 9), "relief": tk.FLAT, "highlightthickness": 1,
                       "highlightbackground": "#e2e8f0", "highlightcolor": "#3b82f6", "bg": "white", "width": 32}

        tk.Label(frame_form, text="Artiste :", bg="#f8fafc", fg="#475569", font=(self.font_family, 9)).grid(row=0, column=0, sticky=tk.E, pady=6)
        entry_art = tk.Entry(frame_form, **entry_style)
        entry_art.insert(0, artiste_defaut)
        entry_art.grid(row=0, column=1, padx=8, pady=6)

        tk.Label(frame_form, text="Album :", bg="#f8fafc", fg="#475569", font=(self.font_family, 9)).grid(row=1, column=0, sticky=tk.E, pady=6)
        entry_alb = tk.Entry(frame_form, **entry_style)
        entry_alb.insert(0, album_defaut)
        entry_alb.grid(row=1, column=1, padx=8, pady=6)

        def valider():
            art = entry_art.get().strip()
            alb = entry_alb.get().strip()
            if not art or not alb:
                messagebox.showwarning("Erreur", "Les deux champs sont obligatoires.", parent=pop)
                return
            pop.destroy()
            self._executer_traitement_album(art, alb)

        frame_btn = tk.Frame(pop, bg="#f8fafc")
        frame_btn.pack(pady=20)
        tk.Button(frame_btn, text=" Lancer", image=self.icons["ed_bolt"], compound=tk.LEFT, command=valider,
                  bg="#f59e0b", fg="white", width=110, font=(self.font_family, 9, "bold"),
                  relief=tk.FLAT, cursor="hand2", pady=6).pack(side=tk.LEFT, padx=10)
        tk.Button(frame_btn, text="Annuler", command=pop.destroy, width=12, bg="#64748b", fg="white",
                  font=(self.font_family, 9, "bold"), relief=tk.FLAT, cursor="hand2", pady=6).pack(side=tk.LEFT, padx=10)

    def _executer_traitement_album(self, artist_name, album_name):
        self.progress_global.pack(side=tk.LEFT, padx=10)
        self.progress_global.start(10)

        def thread_auto_musicbrainz():
            try:
                def maj_statut(msg):
                    self.root.after(0, lambda: self.lbl_info.config(text=msg))

                compte_rendu = []
                backup_batch = {}
                maj_statut("Analyse des fichiers locaux...")
                fichiers = sorted([f for f in os.listdir(self.dossier_actuel) if f.lower().endswith(FORMATS_SUPPORTES_EDITEUR)])
                nb_pistes_locales = len(fichiers)

                if nb_pistes_locales == 0:
                    raise Exception("Aucun fichier audio supporté trouvé.")

                maj_statut(f"Recherche MB ({nb_pistes_locales} pistes)...")
                release = find_release_by_track_count(artist_name, album_name, nb_pistes_locales)

                if not release:
                    raise Exception(f"Aucune édition à {nb_pistes_locales} pistes trouvée pour '{album_name}' de '{artist_name}'.")

                release_id = release["id"]
                maj_statut("Édition trouvée ! Téléchargement de la tracklist...")

                release_data = musicbrainzngs.get_release_by_id(
                    release_id, includes=["recordings", "artist-credits", "labels", "isrcs"]
                )

                album_artist_mb = release_data["release"].get("artist-credit-phrase", artist_name)
                label_info_list = release_data["release"].get("label-info-list", [])
                label_name = ""
                if label_info_list and isinstance(label_info_list[0], dict) and "label" in label_info_list[0]:
                    label_name = label_info_list[0]["label"].get("name", "")

                genre = ""
                original_date = ""
                try:
                    rg_id = release["release-group"]["id"]
                    rg_data = musicbrainzngs.get_release_group_by_id(rg_id, includes=["tags"])
                    original_date = rg_data["release-group"].get("first-release-date", "")[:4]
                    tags = rg_data["release-group"].get("tag-list", [])
                    if tags:
                        tags.sort(key=lambda x: int(x.get("count", 0)), reverse=True)
                        genre = tags[0]["name"].title()
                except Exception:
                    pass

                medium_list = release_data["release"]["medium-list"]
                total_discs = len(medium_list)
                tracks_db = {}

                for medium in medium_list:
                    total_tracks = len(medium["track-list"])
                    disc_position = medium["position"]

                    for track in medium["track-list"]:
                        track_number = int(track["position"])
                        track_key = f"{disc_position}-{track_number}"
                        recording_stub = track["recording"]
                        recording_id = recording_stub["id"]
                        title = recording_stub["title"]
                        track_artist = track.get("artist-credit-phrase", album_artist_mb)

                        maj_statut(f"MB [{track_number}/{total_tracks}] : {title}")
                        time.sleep(1)

                        try:
                            rec_data = musicbrainzngs.get_recording_by_id(
                                recording_id, includes=["artist-rels", "work-rels", "work-level-rels", "isrcs"]
                            )
                            full_recording = rec_data["recording"]
                        except Exception:
                            full_recording = recording_stub

                        isrc_list = full_recording.get("isrc-list", [])
                        isrc_code = ""
                        if isrc_list and isinstance(isrc_list[0], dict):
                            isrc_code = isrc_list[0].get("id", "")

                        composers = []
                        for rel in full_recording.get("artist-relation-list", []):
                            if rel.get("type", "").lower() == "composer" and "artist" in rel:
                                name = rel["artist"].get("name", "")
                                if name not in composers:
                                    composers.append(name)

                        for work_rel in full_recording.get("work-relation-list", []):
                            if "work" in work_rel:
                                for rel in work_rel["work"].get("artist-relation-list", []):
                                    if rel.get("type", "").lower() == "composer" and "artist" in rel:
                                        name = rel["artist"].get("name", "")
                                        if name not in composers:
                                            composers.append(name)

                        duration_ms = int(track.get("length", 0))
                        tracks_db[track_key] = {
                            "title": title, "artist": track_artist, "albumartist": album_artist_mb,
                            "album": release['title'], "tracknumber": f"{track_number}/{total_tracks}",
                            "discnumber": f"{disc_position}/{total_discs}", "date": release.get("date", "")[:4],
                            "originaldate": original_date, "genre": genre, "organization": label_name,
                            "isrc": isrc_code, "composer": ", ".join(composers), "duration": duration_ms / 1000
                        }

                maj_statut("Téléchargement de l'artwork officiel...")
                cover_data = None
                cover_mime = "image/jpeg"
                try:
                    cover_url = f"https://coverartarchive.org/release/{release_id}/front"
                    r_cover = requests.get(cover_url, timeout=10)
                    if r_cover.status_code == 200:
                        cover_data = r_cover.content
                        if 'png' in r_cover.headers.get('Content-Type', '').lower():
                            cover_mime = 'image/png'
                except Exception:
                    pass

                used_tracks = set()
                for index, file in enumerate(fichiers):
                    path = os.path.join(self.dossier_actuel, file)

                    if detecter_faux_mp3(path):
                        maj_statut(f"Conversion à la volée : {file}")
                        path, _ = convertir_en_mp3(path)
                        if not path:
                            compte_rendu.append(f"❌ {file} -> Échec de conversion MP3")
                            continue

                    try:
                        audio_file = mutagen.File(path)
                        duration = audio_file.info.length if audio_file else 0
                    except Exception:
                        duration = 0

                    best_track, score_match = find_best_match(file, duration, tracks_db, used_tracks)

                    if not best_track:
                        compte_rendu.append(f"⚠️ {file} -> Aucun match satisfaisant trouvé")
                        continue

                    used_tracks.add(best_track)
                    metadata = tracks_db[best_track]
                    pourcentage = score_match * 100

                    maj_statut(f"Match [{index+1}/{nb_pistes_locales}] : {file} ➔ {metadata['title']} ({pourcentage:.1f}%)")
                    compte_rendu.append(f"✅ {file}\n   ➔ Assigned: \"{metadata['title']}\" (Match: {pourcentage:.1f}%)")

                    bak = self._creer_backup(path)
                    if bak:
                        backup_batch[path] = bak

                    try:
                        t_bruts = ID3(path)
                    except ID3NoHeaderError:
                        t_bruts = ID3()
                        t_bruts.save(path)
                        t_bruts = ID3(path)
                    for tg in ["TCOP", "WCOP", "TENC", "TSSE", "USER"]:
                        t_bruts.delall(tg)
                    t_bruts.save(path)

                    audio = EasyID3(path)
                    for cp in ["copyright", "license", "encodedby"]:
                        if cp in audio:
                            del audio[cp]

                    audio["title"] = [metadata["title"]]
                    audio["artist"] = [metadata["artist"]]
                    audio["albumartist"] = [metadata["albumartist"]]
                    audio["album"] = [metadata["album"]]
                    audio["tracknumber"] = [metadata["tracknumber"]]
                    audio["discnumber"] = [metadata["discnumber"]]
                    if metadata["date"]: audio["date"] = [metadata["date"]]
                    if metadata["originaldate"]: audio["originaldate"] = [metadata["originaldate"]]
                    if metadata["genre"]: audio["genre"] = [metadata["genre"]]
                    if metadata["organization"]: audio["organization"] = [metadata["organization"]]
                    if metadata["isrc"]: audio["isrc"] = [metadata["isrc"]]
                    if metadata["composer"]: audio["composer"] = [metadata["composer"]]
                    if metadata["albumartist"].lower() in ["various artists", "divers"]:
                        audio["compilation"] = ["1"]
                    audio.save()

                    if cover_data:
                        t_id3 = ID3(path)
                        t_id3.delall("APIC")
                        t_id3.add(APIC(encoding=3, mime=cover_mime, type=3, desc="Cover", data=cover_data))
                        t_id3.save(path)

                self._ajouter_historique(backup_batch)

                def finaliser():
                    self.progress_global.stop()
                    self.progress_global.pack_forget()
                    maj_statut("Opération terminée !")
                    self.charger_fichiers()
                    self._afficher_rapport_scores(album_name, compte_rendu)

                self.root.after(0, finaliser)

            except Exception as e:
                def lever_erreur(msg):
                    self.progress_global.stop()
                    self.progress_global.pack_forget()
                    maj_statut("Échec du Mode Auto.")
                    messagebox.showerror("Erreur Mode Auto", msg)
                self.root.after(0, lambda: lever_erreur(str(e)))

        threading.Thread(target=thread_auto_musicbrainz, daemon=True).start()

    def _afficher_rapport_scores(self, album, lignes_rapport):
        rep = tk.Toplevel(self.root)
        rep.title("Rapport de Correspondance MusicBrainz")
        rep.geometry("620x450")
        rep.configure(bg="#f8fafc")
        rep.transient(self.root)
        rep.grab_set()

        tk.Label(rep, text=f"Bilan du tag automatique : {album}", font=(self.font_family, 11, "bold"), bg="#f8fafc", fg="#1e293b").pack(pady=12)

        f_txt = tk.Frame(rep, bg="#f8fafc")
        f_txt.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)

        txt = tk.Text(f_txt, wrap=tk.WORD, font=("Consolas", 10), bg="#ffffff", relief=tk.FLAT, highlightthickness=1, highlightbackground="#e2e8f0")
        sb = ttk.Scrollbar(f_txt, orient=tk.VERTICAL, command=txt.yview, style="Modern.Vertical.TScrollbar")
        txt.configure(yscrollcommand=sb.set)

        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        for l in lignes_rapport:
            txt.insert(tk.END, l + "\n\n")
        txt.config(state=tk.DISABLED)

        tk.Button(rep, text="Fermer le rapport", command=rep.destroy, width=16, bg="#3b82f6", fg="white",
                  font=(self.font_family, 9, "bold"), relief=tk.FLAT, cursor="hand2", pady=6).pack(pady=14)

    # =========================================================
    # BRANCHE B : MODE MULTI-SINGLES VRAC (DIVERS / NA)
    # =========================================================

    def _lancer_interface_vrac(self, nom_dossier):
        """Ouvre l'application d'édition en vrac sous forme de sous-fenêtre modale (Toplevel)."""
        vrac_win = tk.Toplevel(self.root)
        vrac_win.title(f"Mode Spécial Vrac — Dossier détecté : [{nom_dossier}]")
        vrac_win.geometry("1020x720")
        vrac_win.configure(bg="#f8fafc")
        vrac_win.transient(self.root)
        vrac_win.grab_set()

        lbl_intro = tk.Label(vrac_win, text=f"Analyse unitaire en cours dans '{os.path.basename(self.dossier_actuel)}' (Album : Divers)",
                              font=(self.font_family, 11, "bold"), bg="#f8fafc", fg="#3b82f6")
        lbl_intro.pack(pady=12)

        progress = ttk.Progressbar(vrac_win, length=960)
        progress.pack(pady=5)

        list_container = tk.Frame(vrac_win, bd=1, relief=tk.SOLID, bg="#e2e8f0")
        list_container.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)

        canvas = tk.Canvas(list_container, bg="#f8fafc", highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview, style="Modern.Vertical.TScrollbar")
        scrollable_frame = tk.Frame(canvas, bg="#f8fafc")

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def _on_mousewheel_vrac(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        vrac_win.bind("<MouseWheel>", _on_mousewheel_vrac)

        bottom_frame = tk.Frame(vrac_win, bg="#f8fafc")
        bottom_frame.pack(fill=tk.X, pady=12, padx=15)

        log_text = tk.Text(bottom_frame, height=5, bg="#f1f5f9", font=("Consolas", 9), relief=tk.FLAT, highlightthickness=1, highlightbackground="#e2e8f0")
        log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 15))

        def log_vrac(msg):
            log_text.insert(tk.END, msg + "\n")
            log_text.see(tk.END)

        apply_button = tk.Button(bottom_frame, text="Appliquer les tags\nsur les lignes cochées", bg="#10b981", fg="white",
                                  width=22, height=3, relief=tk.FLAT, font=(self.font_family, 9, "bold"), state=tk.DISABLED)
        apply_button.pack(side=tk.RIGHT)

        proposed_tags_vrac = []

        def thread_analyse_vrac():
            try:
                log_vrac("Lecture des fichiers locaux du dossier...")
                files = [f for f in os.listdir(self.dossier_actuel) if f.lower().endswith(".mp3")]

                if not files:
                    messagebox.showerror("Erreur", "Aucun fichier MP3 trouvé pour l'analyse en vrac.", parent=vrac_win)
                    return

                progress["maximum"] = len(files)
                progress["value"] = 0

                tk.Label(scrollable_frame, text="Valider", font=(self.font_family, 9, "bold"), bg="#f8fafc", fg="#1e293b").grid(row=0, column=0, padx=6, pady=5)
                tk.Label(scrollable_frame, text="Fichier Source", font=(self.font_family, 9, "bold"), bg="#f8fafc", fg="#1e293b").grid(row=0, column=1, sticky="w", padx=6, pady=5)
                tk.Label(scrollable_frame, text="Titre Suggéré", font=(self.font_family, 9, "bold"), bg="#f8fafc", fg="#1e293b").grid(row=0, column=2, sticky="w", padx=6, pady=5)
                tk.Label(scrollable_frame, text="Artiste Suggéré", font=(self.font_family, 9, "bold"), bg="#f8fafc", fg="#1e293b").grid(row=0, column=3, sticky="w", padx=6, pady=5)
                tk.Label(scrollable_frame, text="Année", font=(self.font_family, 9, "bold"), bg="#f8fafc", fg="#1e293b").grid(row=0, column=4, sticky="w", padx=6, pady=5)

                for index, file in enumerate(files):
                    path = os.path.join(self.dossier_actuel, file)
                    filename_clean = os.path.splitext(file)[0]

                    if "-" in filename_clean:
                        parts = filename_clean.split("-", 1)
                        raw_artist = parts[0].strip()
                        raw_title = parts[1].replace("-", " ").strip()
                    else:
                        raw_artist = ""
                        raw_title = filename_clean.replace("-", " ").strip()

                    search_artist = clean_youtube_name(raw_artist)
                    search_title = clean_youtube_name(raw_title)

                    true_title = search_title
                    true_artist = search_artist
                    true_year = ""

                    cache_key = f"{search_artist}||{search_title}"
                    if cache_key in self.api_cache:
                        true_title, true_artist, true_year = self.api_cache[cache_key]
                        log_vrac(f"⚡ {search_title} -> Extrait du Cache Local instantanément !")
                    else:
                        log_vrac(f"Recherche unitaire : {search_title}...")
                        time.sleep(1)

                        try:
                            requete = f'(recording:"{search_title}" AND artist:"{search_artist}") OR (recording:"{search_artist}" AND artist:"{search_title}")'
                            res = musicbrainzngs.search_recordings(query=requete, limit=3)
                            recordings = res.get("recording-list", [])

                            if recordings:
                                best_match = recordings[0]
                                score = int(best_match.get("ext:score", 0))
                                log_vrac(f"  -> Correspondance trouvée ({score}%)")

                                true_title = best_match.get("title", search_title)
                                true_artist = best_match.get("artist-credit-phrase", search_artist)

                                release_list = best_match.get("release-list", [])
                                if release_list and "date" in release_list[0]:
                                    true_year = release_list[0]["date"][:4]

                                self.api_cache[cache_key] = (true_title, true_artist, true_year)
                            else:
                                log_vrac("  -> Pas de match exact. Nettoyage local conservé.")

                        except Exception as e:
                            log_vrac(f"Erreur MusicBrainz pour {file} : {e}")

                    check_var = tk.BooleanVar(value=False)
                    title_var = tk.StringVar(value=true_title)
                    artist_var = tk.StringVar(value=true_artist)
                    year_var = tk.StringVar(value=true_year)

                    row_idx = index + 1
                    tk.Checkbutton(scrollable_frame, variable=check_var, bg="#f8fafc", activebackground="#f8fafc").grid(row=row_idx, column=0, padx=6, pady=3)

                    lbl_txt = file if len(file) < 32 else file[:29] + "..."
                    tk.Label(scrollable_frame, text=lbl_txt, width=28, anchor="w", bg="#f8fafc", fg="#64748b", font=(self.font_family, 9)).grid(row=row_idx, column=1, padx=6, pady=3)

                    tk.Entry(scrollable_frame, textvariable=title_var, width=28, relief=tk.FLAT, highlightthickness=1,
                             highlightbackground="#e2e8f0", highlightcolor="#3b82f6", font=(self.font_family, 9)).grid(row=row_idx, column=2, padx=6, pady=3)
                    tk.Entry(scrollable_frame, textvariable=artist_var, width=26, relief=tk.FLAT, highlightthickness=1,
                             highlightbackground="#e2e8f0", highlightcolor="#3b82f6", font=(self.font_family, 9)).grid(row=row_idx, column=3, padx=6, pady=3)
                    tk.Entry(scrollable_frame, textvariable=year_var, width=6, relief=tk.FLAT, highlightthickness=1,
                             highlightbackground="#e2e8f0", highlightcolor="#3b82f6", font=(self.font_family, 9)).grid(row=row_idx, column=4, padx=6, pady=3)

                    proposed_tags_vrac.append({
                        "path": path, "check_var": check_var, "title_var": title_var,
                        "artist_var": artist_var, "year_var": year_var
                    })

                    progress["value"] = index + 1

                log_vrac("✅ Analyse terminée ! Vérifiez les lignes puis cochez celles à sauvegarder.")
                apply_button.config(state=tk.NORMAL)

            except Exception as ex:
                messagebox.showerror("Erreur Thread Vrac", str(ex), parent=vrac_win)

        def action_appliquer_tags_vrac():
            count = 0
            backup_batch = {}
            try:
                for i, item in enumerate(proposed_tags_vrac):
                    if item["check_var"].get():
                        path = item["path"]

                        bak = self._creer_backup(path)
                        if bak:
                            backup_batch[path] = bak

                        try:
                            audio = mutagen.mp3.MP3(path, ID3=EasyID3)
                        except Exception:
                            audio = mutagen.mp3.MP3(path)
                            audio.add_tags(ID3=EasyID3)

                        audio["title"] = item["title_var"].get().strip()
                        audio["artist"] = item["artist_var"].get().strip()

                        year = item["year_var"].get().strip()
                        if year:
                            audio["date"] = year

                        audio["album"] = "Divers"
                        audio["albumartist"] = "Various Artists"
                        audio["tracknumber"] = str(i + 1)
                        audio["discnumber"] = "1/1"

                        audio.save()
                        count += 1

                self._ajouter_historique(backup_batch)
                messagebox.showinfo("Succès", f"{count} fichier(s) mis à jour avec succès !", parent=vrac_win)
                self.charger_fichiers()
                vrac_win.destroy()

            except Exception as e:
                messagebox.showerror("Erreur d'écriture", str(e), parent=vrac_win)

        apply_button.config(command=action_appliquer_tags_vrac)
        threading.Thread(target=thread_analyse_vrac, daemon=True).start()

    # =========================================================
    # LOGIQUE DE GESTION DES IMAGES & COVERS
    # =========================================================

    def _lire_pochette(self, chemin):
        try:
            tags = ID3(chemin)
            for key in tags:
                if key.startswith("APIC"):
                    return tags[key].data, tags[key].mime
        except Exception:
            pass
        return None, None

    def _afficher_pochette(self, chemin):
        data, _ = self._lire_pochette(chemin)
        if data:
            try:
                img = Image.open(io.BytesIO(data)).resize((88, 88), Image.Resampling.LANCZOS)
                self.photo_img = ImageTk.PhotoImage(img, master=self.root)
                self.lbl_pochette.config(image=self.photo_img, text="", width=88, height=88, bg="#e2e8f0")
                return
            except Exception:
                pass
        self.photo_img = None
        self.lbl_pochette.config(image="", text="Pas de\npochette", width=11, height=5, fg="#94a3b8", bg="#e2e8f0")

    def choose_cover(self):
        chemin = filedialog.askopenfilename(
            title="Choisir une image de pochette",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp"), ("Tous les fichiers", "*.*")])
        if not chemin:
            return
        try:
            with open(chemin, "rb") as f:
                self.nouvelle_pochette_data = f.read()
        except OSError as e:
            messagebox.showerror("Erreur", f"Impossible de lire l'image :\n{e}")
            return
        ext = os.path.splitext(chemin)[1].lower()
        self.nouvelle_pochette_mime = "image/png" if ext == ".png" else "image/jpeg"
        try:
            img = Image.open(chemin).resize((88, 88), Image.Resampling.LANCZOS)
            self.photo_img = ImageTk.PhotoImage(img, master=self.root)
            self.lbl_pochette.config(image=self.photo_img, text="", width=88, height=88)
            return
        except Exception:
            pass
        self.lbl_pochette.config(image="", text="Nouvelle\npochette ✔", fg="#10b981", width=11, height=5)

    def delete_cover(self):
        self.nouvelle_pochette_data = b""
        self.nouvelle_pochette_mime = None
        self.photo_img = None
        self.lbl_pochette.config(image="", text="Supprimée\n🗑", fg="#ef4444", width=11, height=5)

    def _drag_debut(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            self._drag_anchor = None
            return
        item = self.tree.identify_row(event.y)
        if not item:
            self._drag_anchor = None
            return
        self._drag_anchor = item
        self.tree.selection_set(item)
        self.tree.focus(item)
        return "break"

    def _drag_mouvement(self, event):
        if not self._drag_anchor:
            return
        item_courant = self.tree.identify_row(event.y)
        if not item_courant:
            return
        tous = list(self.tree.get_children())
        try:
            a = tous.index(self._drag_anchor)
            b = tous.index(item_courant)
        except ValueError:
            return
        debut, fin = min(a, b), max(a, b)
        self.tree.selection_set(tous[debut: fin + 1])
        self.tree.see(item_courant)
        return "break"


class YoutubeTab:
    """
    Onglet "YouTube" : affiche les suggestions de la page d'accueil YouTube Music (Sélection
    rapide, Découverte du jour, Nouveautés...) et pilote la lecture en
    tampon (voir youtube_manager.py). Construit son interface dans `parent` (fourni par
    MusicPlayer), sur le même principe que MetadataEditor -- ne touche jamais le moteur audio
    directement, délègue toute la lecture à `player` (l'instance MusicPlayer).
    """

    def __init__(self, parent, root, font_family, player):
        self.parent = parent
        self.root = root
        self.font_family = font_family
        self.player = player  # instance de MusicPlayer

        self.thumbnail_cache = {}  # url vignette -> PhotoImage (évite de retélécharger)
        self._session_expired_shown = False  # évite de reprévenir en boucle toutes les 2s (voir _status_loop)

        self._build_ui()
        self._status_loop()

    # ------------------------------------------------------------------
    # CONSTRUCTION DE L'INTERFACE
    # ------------------------------------------------------------------
    def _build_ui(self):
        header = tk.Frame(self.parent, bg="#f8fafc")
        header.pack(fill=tk.X, padx=20, pady=(15, 5))

        tk.Label(header, text="🎵 YouTube Music", font=(self.font_family, 16, "bold"),
                 bg="#f8fafc", fg="#1e293b").pack(side=tk.LEFT)

        self.lbl_status = tk.Label(header, text="Non connecté", font=(self.font_family, 10),
                                    bg="#f8fafc", fg="#94a3b8")
        self.lbl_status.pack(side=tk.LEFT, padx=15)

        self.btn_connect = tk.Button(
            header, text="🔌 Se connecter", font=(self.font_family, 10, "bold"),
            bg="#dc2626", fg="white", relief=tk.FLAT, cursor="hand2", command=self.connect
        )
        self.btn_connect.pack(side=tk.RIGHT, padx=5)

        self.btn_refresh = tk.Button(
            header, text="🔄 Actualiser", font=(self.font_family, 10, "bold"),
            bg="#3b82f6", fg="white", relief=tk.FLAT, cursor="hand2",
            command=self.refresh_selection, state=tk.DISABLED
        )
        self.btn_refresh.pack(side=tk.RIGHT, padx=5)

        # --- Barre de statut du tampon (mise à jour périodiquement) ---
        buffer_bar = tk.Frame(self.parent, bg="#f1f5f9")
        buffer_bar.pack(fill=tk.X, padx=20, pady=(0, 10))

        self.lbl_buffer = tk.Label(buffer_bar, text="Tampon : —", font=(self.font_family, 9, "italic"),
                                    bg="#f1f5f9", fg="#64748b")
        self.lbl_buffer.pack(side=tk.LEFT, padx=10, pady=6)

        self.btn_radio = tk.Button(
            buffer_bar, text="📻 Lancer la radio (tampon)", font=(self.font_family, 9, "bold"),
            bg="#10b981", fg="white", relief=tk.FLAT, cursor="hand2",
            command=self.start_radio, state=tk.DISABLED
        )
        self.btn_radio.pack(side=tk.RIGHT, padx=10, pady=4)

        # --- Liste des suggestions, regroupées par section (scrollable) ---
        list_container = tk.Frame(self.parent, bg="#f8fafc")
        list_container.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 15))

        canvas = tk.Canvas(list_container, bg="#f8fafc", highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient=tk.VERTICAL, command=canvas.yview,
                                   style="Modern.Vertical.TScrollbar")
        self.list_frame = tk.Frame(canvas, bg="#f8fafc")

        self.list_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.lbl_empty = tk.Label(
            self.list_frame, text="Connecte-toi à ton compte YouTube Music pour voir tes suggestions.",
            font=(self.font_family, 10), bg="#f8fafc", fg="#94a3b8", wraplength=500, justify="center"
        )
        self.lbl_empty.pack(pady=40)

    # ------------------------------------------------------------------
    # CONNEXION
    # ------------------------------------------------------------------
    def connect(self):
        """Ouvre un petit choix entre les deux méthodes d'authentification supportées par
        ytmusicapi : OAuth (Google Cloud, nécessite un Client ID/Secret) ou Navigateur
        (headers copiés depuis music.youtube.com, plus rapide à mettre en place mais peut
        expirer si la session du navigateur se termine)."""
        if self.player.youtube_manager is not None and not getattr(self.player.youtube_manager, "session_expired", False):
            return  # déjà connecté avec une session valide

        choice_win = tk.Toplevel(self.root)
        choice_win.title("Méthode de connexion")
        choice_win.geometry("380x200")
        choice_win.transient(self.root)
        choice_win.grab_set()

        tk.Label(choice_win, text="Comment veux-tu te connecter à YouTube Music ?",
                 font=(self.font_family, 11, "bold"), wraplength=340, justify="center").pack(pady=(20, 15))

        def pick(method):
            choice_win.destroy()
            if method == "oauth":
                self._connect_oauth()
            else:
                self._connect_browser()

        tk.Button(
            choice_win, text="🔑 OAuth (Google Cloud)\nplus robuste, demande un Client ID/Secret",
            font=(self.font_family, 9, "bold"), bg="#3b82f6", fg="white", relief=tk.FLAT,
            cursor="hand2", justify="center", command=lambda: pick("oauth")
        ).pack(fill=tk.X, padx=20, pady=6)

        tk.Button(
            choice_win, text="🌐 Navigateur (headers)\nplus rapide, à refaire si la session expire",
            font=(self.font_family, 9, "bold"), bg="#64748b", fg="white", relief=tk.FLAT,
            cursor="hand2", justify="center", command=lambda: pick("browser")
        ).pack(fill=tk.X, padx=20, pady=6)

    def _connect_oauth(self):
        """Méthode OAuth : nécessite oauth.json + Client ID/Secret Google Cloud."""
        oauth_path = filedialog.askopenfilename(
            title="Sélectionne ton fichier oauth.json (ytmusicapi)",
            filetypes=[("Fichier JSON", "*.json"), ("Tous les fichiers", "*.*")]
        )
        if not oauth_path:
            return

        client_id, client_secret = self._get_client_credentials()
        if not client_id or not client_secret:
            return  # annulé par l'utilisateur

        self._start_connection(lambda: YouTubeManager(
            oauth_path=oauth_path, client_id=client_id, client_secret=client_secret
        ))

    def _connect_browser(self):
        """Méthode navigateur : un seul fichier (ex: browser.json généré par `ytmusicapi browser`),
        pas de Client ID/Secret nécessaire."""
        browser_path = filedialog.askopenfilename(
            title="Sélectionne ton fichier browser.json (ytmusicapi browser)",
            filetypes=[("Fichier JSON", "*.json"), ("Tous les fichiers", "*.*")]
        )
        if not browser_path:
            return

        self._start_connection(lambda: YouTubeManager(oauth_path=browser_path))

    def _start_connection(self, build_manager):
        """Facteur commun aux deux méthodes : instancie le manager dans un thread (opération
        réseau) puis met à jour la GUI sur le thread principal."""
        self.btn_connect.config(state=tk.DISABLED, text="Connexion...")
        self.lbl_status.config(text="Connexion en cours...", fg="#f59e0b")

        def worker():
            try:
                manager = build_manager()
                compte = manager.verifier_compte()
                self.root.after(0, lambda: self._on_connected(manager, compte))
            except Exception as e:
                self.root.after(0, lambda: self._on_connect_error(str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _get_client_credentials(self, config_path="youtube_client_credentials.json"):
        """Récupère le Client ID / Secret Google Cloud depuis un fichier local s'il existe déjà,
        sinon les demande à l'utilisateur et les enregistre pour les prochaines connexions."""
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("client_id") and data.get("client_secret"):
                    return data["client_id"], data["client_secret"]
            except (json.JSONDecodeError, OSError):
                pass

        messagebox.showinfo(
            "Identifiants Google Cloud requis",
            "Depuis novembre 2024, YouTube Music exige un Client ID et un Client Secret "
            "(créés sur console.cloud.google.com, type \"TVs and Limited Input devices\").\n\n"
            "Tu vas devoir les saisir une seule fois : ils seront enregistrés localement "
            "pour les prochaines connexions.",
            parent=self.root,
        )
        client_id = simpledialog.askstring("Client ID", "Colle ton Client ID Google Cloud :", parent=self.root)
        if not client_id:
            return None, None
        client_secret = simpledialog.askstring(
            "Client Secret", "Colle ton Client Secret Google Cloud :", parent=self.root, show="*"
        )
        if not client_secret:
            return None, None

        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({"client_id": client_id, "client_secret": client_secret}, f)
        except OSError:
            pass  # tant pis, il faudra les resaisir la prochaine fois

        return client_id, client_secret

    def _on_connected(self, manager, compte=None):
        old_manager = self.player.youtube_manager
        if old_manager is not None and old_manager is not manager:
            # Ancienne session (expirée) remplacée : on l'arrête proprement (thread + cache
            # temporaire) plutôt que de la laisser orpheline en arrière-plan.
            try:
                old_manager.stop()
            except Exception:
                pass

        self.player.youtube_manager = manager
        self._session_expired_shown = False

        if compte:
            self.lbl_status.config(text=f"✅ Connecté ({compte})", fg="#10b981")
        elif compte == "":
            self.lbl_status.config(text="✅ Connecté (compte vérifié)", fg="#10b981")
        else:
            # verifier_compte() a retourné None : la connexion a réussi techniquement (pas
            # d'exception), mais rien ne prouve qu'elle est liée à un compte personnel plutôt
            # qu'à une session anonyme -- ce qui expliquerait des recommandations génériques.
            self.lbl_status.config(text="⚠️ Connecté, compte non vérifié", fg="#f59e0b")
            messagebox.showwarning(
                "YouTube Music",
                "La connexion a réussi techniquement, mais je n'ai pas pu confirmer qu'elle est "
                "bien liée à ton compte personnel. Les suggestions affichées pourraient être "
                "génériques plutôt que basées sur ton historique.\n\n"
                "Vérifie que ton fichier contient bien un cookie de session valide et complet, "
                "ou essaie la méthode OAuth (plus fiable, moins sujette aux erreurs de copier-coller).",
                parent=self.root,
            )

        self.btn_connect.config(text="🔌 Connecté", state=tk.DISABLED)
        self.btn_refresh.config(state=tk.NORMAL)
        self.btn_radio.config(state=tk.NORMAL)
        self.refresh_selection()

    def _on_connect_error(self, message):
        self.lbl_status.config(text="❌ Échec de connexion", fg="#ef4444")
        self.btn_connect.config(state=tk.NORMAL, text="🔌 Se connecter")
        messagebox.showerror("Connexion YouTube Music", message, parent=self.root)

    # ------------------------------------------------------------------
    # SÉLECTION RAPIDE
    # ------------------------------------------------------------------
    def refresh_selection(self):
        if not self.player.youtube_manager:
            return
        self.btn_refresh.config(state=tk.DISABLED, text="Chargement...")

        def worker():
            error_msg = None
            try:
                items = self.player.youtube_manager.get_selection_rapide()
            except Exception as e:
                items, error_msg = [], str(e)
            self.root.after(0, lambda: self._render_selection(items, error_msg))

        threading.Thread(target=worker, daemon=True).start()

    def _render_selection(self, items, error_msg):
        self.btn_refresh.config(state=tk.NORMAL, text="🔄 Actualiser")
        for child in self.list_frame.winfo_children():
            child.destroy()

        if error_msg:
            tk.Label(self.list_frame, text=f"Erreur lors du chargement : {error_msg}",
                     font=(self.font_family, 10), bg="#f8fafc", fg="#ef4444", wraplength=500).pack(pady=30)
            return

        if not items:
            tk.Label(self.list_frame, text="Aucune suggestion trouvée pour le moment.",
                     font=(self.font_family, 10), bg="#f8fafc", fg="#94a3b8").pack(pady=30)
            return

        # Regroupe par section (Sélection rapide, Découverte du jour, Nouveautés...) en
        # conservant l'ordre d'apparition tel que renvoyé par la page d'accueil YouTube Music.
        sections = {}
        ordre_sections = []
        for item in items:
            section = item.get("section") or "Suggestions"
            if section not in sections:
                sections[section] = []
                ordre_sections.append(section)
            sections[section].append(item)

        for i, section in enumerate(ordre_sections):
            tk.Label(
                self.list_frame, text=section, font=(self.font_family, 12, "bold"),
                bg="#f8fafc", fg="#1e293b", anchor="w"
            ).pack(fill=tk.X, padx=4, pady=(18 if i > 0 else 4, 6))
            for item in sections[section]:
                self._build_track_row(item)

    def _build_track_row(self, item):
        row = tk.Frame(self.list_frame, bg="white", highlightbackground="#e2e8f0", highlightthickness=1)
        row.pack(fill=tk.X, pady=4, padx=2)

        thumb_label = tk.Label(row, bg="#e2e8f0", width=6, height=3)
        thumb_label.pack(side=tk.LEFT, padx=10, pady=8)
        self._load_thumbnail(item.get("vignette"), thumb_label)

        info_frame = tk.Frame(row, bg="white")
        info_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        tk.Label(info_frame, text=item["titre"], font=(self.font_family, 10, "bold"),
                 bg="white", fg="#1e293b", anchor="w").pack(fill=tk.X)
        tk.Label(info_frame, text=item["artiste"], font=(self.font_family, 9),
                 bg="white", fg="#64748b", anchor="w").pack(fill=tk.X)

        btn_play = tk.Button(
            row, text="▶ Écouter", font=(self.font_family, 9, "bold"), bg="#3b82f6", fg="white",
            relief=tk.FLAT, cursor="hand2"
        )
        btn_play.config(command=lambda it=item, b=btn_play: self.play_now(it, b))
        btn_play.pack(side=tk.RIGHT, padx=12)

    def _load_thumbnail(self, url, label_widget):
        if not url:
            return
        if url in self.thumbnail_cache:
            label_widget.config(image=self.thumbnail_cache[url], width=60, height=60)
            return

        def worker():
            try:
                resp = requests.get(url, timeout=8)
                img = Image.open(io.BytesIO(resp.content)).resize((60, 60), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(img, master=self.root)

                def apply():
                    self.thumbnail_cache[url] = photo
                    if label_widget.winfo_exists():
                        label_widget.config(image=photo, width=60, height=60)
                self.root.after(0, apply)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # LECTURE
    # ------------------------------------------------------------------
    def play_now(self, item, btn=None):
        """Télécharge (si besoin) et joue immédiatement un morceau choisi dans la Sélection
        rapide. Le téléchargement se fait dans un thread pour ne pas geler la GUI ; le bouton
        cliqué se met à jour immédiatement pour montrer que quelque chose se passe (le premier
        téléchargement d'un morceau peut prendre 10-20 secondes)."""
        if not self.player.youtube_manager:
            return

        if btn is not None:
            btn.config(state=tk.DISABLED, text="⏳ Téléchargement...", bg="#94a3b8")

        def worker():
            error_msg = self.player.play_youtube_item_now(item)
            if btn is not None:
                self.root.after(0, lambda: self._reset_play_button(btn, error_msg))

        threading.Thread(target=worker, daemon=True).start()

    def _reset_play_button(self, btn, error_msg):
        if not btn.winfo_exists():
            return
        btn.config(state=tk.NORMAL, text="▶ Écouter", bg="#3b82f6")
        if error_msg:
            messagebox.showerror("YouTube", error_msg, parent=self.root)

    def start_radio(self):
        """Démarre/poursuit la lecture depuis le tampon (file continue de recommandations)."""
        self.player.play_next_from_youtube_buffer(manual=True)

    # ------------------------------------------------------------------
    # STATUT DU TAMPON (polling)
    # ------------------------------------------------------------------
    def _status_loop(self):
        manager = self.player.youtube_manager
        if manager:
            try:
                ready = manager.ready_queue.qsize()
                maxsize = manager.ready_queue.maxsize
                self.lbl_buffer.config(text=f"Tampon : {ready}/{maxsize} morceau(x) prêt(s)")
            except Exception:
                pass

            if getattr(manager, "session_expired", False) and not self._session_expired_shown:
                self._session_expired_shown = True
                self._handle_session_expired()

        self.root.after(2000, self._status_loop)

    def _handle_session_expired(self):
        """Prévient clairement l'utilisateur quand la session YouTube Music a expiré, au lieu de
        laisser l'appli accumuler des échecs silencieux en arrière-plan (voir README, section
        Robustesse). La lecture depuis le tampon déjà en cache n'est pas interrompue : seule la
        récupération de nouvelles recommandations s'arrête (voir youtube_manager.session_expired)."""
        self.lbl_status.config(text="⚠️ Session expirée", fg="#ef4444")
        self.btn_connect.config(state=tk.NORMAL, text="🔌 Se reconnecter")
        self.btn_refresh.config(state=tk.DISABLED)
        self.btn_radio.config(state=tk.DISABLED)
        messagebox.showwarning(
            "YouTube Music",
            "Ta session YouTube Music a expiré (identifiants invalides ou cookie périmé).\n\n"
            "La lecture des morceaux déjà en cache continue de fonctionner, mais aucune nouvelle "
            "recommandation ne sera récupérée tant que tu ne te reconnectes pas.",
            parent=self.root,
        )


if __name__ == "__main__":
    root = tk.Tk()
    app = MusicPlayer(root)
    root.mainloop()
