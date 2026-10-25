"""
YouTubeManager - Version robuste
==================================
Gestionnaire unifié pour YouTube Music : Sélection rapide + cache MP3 en tampon.

Améliorations apportées à la version initiale :
- Anti-doublons (mémoire de session + historique persistant sur disque)
- Retries avec backoff sur les appels réseau (API + téléchargement)
- Arrêt propre du thread même s'il est bloqué sur queue.put()
- Nettoyage automatique du cache (limite de taille + suppression après lecture)
- Logging structuré au lieu d'exceptions avalées silencieusement
"""

import os
import json
import time
import shutil
import logging
import tempfile
import threading
import queue
from typing import Optional, Callable, Any

import yt_dlp
from ytmusicapi import YTMusic, OAuthCredentials

logger = logging.getLogger("youtube_manager")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class YouTubeManager:
    """
    Gestionnaire unifié pour YouTube Music.

    1. Interaction avec l'API pour l'IHM (ex: récupérer la "Sélection rapide").
    2. Mise en cache MP3 en arrière-plan avec un système de tampon à 3 morceaux,
       anti-doublons, retries, et nettoyage automatique du cache.
    """

    def __init__(
        self,
        oauth_path: str = "oauth.json",
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        buffer_size: int = 3,
        max_cache_mb: int = 200,
        history_path: str = "download_history.json",
        max_retries: int = 3,
        history_save_every: int = 5,
    ):
        # Depuis novembre 2024, YouTube Music exige un Client ID + Client Secret
        # (créés sur Google Cloud Console, type "TVs and Limited Input devices")
        # en plus du fichier oauth.json généré par `ytmusicapi oauth`.
        if client_id and client_secret:
            self.yt = YTMusic(
                oauth_path,
                oauth_credentials=OAuthCredentials(client_id=client_id, client_secret=client_secret),
            )
        else:
            # Rétrocompatibilité : fonctionne seulement si l'installation de ytmusicapi
            # est antérieure à ce changement, ou si un client par défaut est encore actif.
            self.yt = YTMusic(oauth_path)

        self.ready_queue = queue.Queue(maxsize=buffer_size)
        self.temp_dir = tempfile.mkdtemp(prefix="lecteur_pro_yt_")
        self.max_cache_bytes = max_cache_mb * 1024 * 1024
        self.max_retries = max_retries

        # --- Anti-doublons ---
        # En mémoire pour la session courante : évite de remettre en queue
        # un morceau déjà téléchargé/en cours dans cette exécution.
        self.seen_ids = set()
        # Historique persistant : évite de retélécharger des morceaux déjà
        # écoutés lors de sessions précédentes (optionnel mais utile si
        # les recommandations reviennent souvent sur les mêmes titres).
        self.history_path = history_path
        self.history_ids = self._load_history()
        self.history_save_every = history_save_every
        self._downloads_since_save = 0

        # Contrôle du thread : is_running est vérifié entre les opérations, _stop_event
        # permet en plus d'interrompre immédiatement une attente/backoff en cours (voir
        # _interruptible_sleep) au lieu de devoir la laisser s'écouler jusqu'au bout.
        self.is_running = True
        self._stop_event = threading.Event()

        self.reco_generator = self._get_recommendations_generator()
        self.loader_thread = threading.Thread(target=self._background_loader, daemon=True)
        self.loader_thread.start()

    # =========================================================================
    # PARTIE 1 : DONNÉES POUR L'INTERFACE GRAPHIQUE (IHM)
    # =========================================================================

    def verifier_compte(self):
        """Vérifie (best-effort) que la session est bien liée à un compte YouTube Music
        personnel, plutôt qu'un mode anonyme/générique qui peut répondre à get_home() sans
        erreur mais avec des recommandations non personnalisées. Essaie d'abord de récupérer
        le nom du compte (le plus parlant pour l'utilisateur) ; si l'API installée ne propose
        pas cette méthode, retombe sur un appel qui n'a de sens que pour un compte connecté
        (la bibliothèque personnelle n'existe pas en mode anonyme).

        Retourne :
            - le nom du compte (str) si disponible,
            - "" si la vérification a réussi mais sans nom exploitable,
            - None si la vérification a échoué (session probablement anonyme/invalide).
        """
        try:
            info = self.yt.get_account_info()
            nom = (info or {}).get("accountName") or (info or {}).get("channelHandle") or ""
            return nom
        except Exception as e:
            logger.debug("get_account_info() indisponible ou a échoué : %s", e)

        try:
            self.yt.get_library_playlists(limit=1)
            return ""
        except Exception as e:
            logger.warning("Impossible de confirmer que la session est liée à un compte personnel : %s", e)
            return None

    def get_selection_rapide(self) -> list:
        """
        Interroge la page d'accueil de YouTube Music et extrait TOUTES les sections contenant
        des morceaux directement jouables (Sélection rapide, Découverte du jour, Nouveautés,
        et toute autre section future de ce type) -- sans dépendre du libellé exact de chaque
        section, qui varie selon la langue du compte et l'algorithme de recommandation.
        Chaque morceau renvoyé porte une clé "section" (le titre réel de la section tel que
        renvoyé par YouTube Music) pour que l'IHM puisse les regrouper visuellement.

        Limite connue : certaines sections de la page d'accueil (mix générés, albums mis en
        avant...) ne listent pas des morceaux individuels mais des conteneurs (playlistId/
        browseId sans videoId direct). Ces éléments sont ignorés ici -- les développer
        demanderait un appel API supplémentaire par élément (get_playlist/get_album), non fait
        pour rester simple et rapide.
        """
        try:
            home_data = self._call_with_retry(self.yt.get_home, limit=10)
        except Exception as e:
            logger.error("Impossible de récupérer la page d'accueil : %s", e)
            return []

        liste_suggestions = []
        sections_vues = []

        for section in home_data:
            titre_section = section.get("title") or "Suggestions"
            if titre_section not in sections_vues:
                sections_vues.append(titre_section)

            for item in section.get("contents", []):
                video_id = item.get("videoId")
                if not video_id:
                    continue  # conteneur (mix/album/playlist) sans morceau direct -- ignoré, voir docstring

                titre = item.get("title", "Titre inconnu")
                artistes = ", ".join(
                    a.get("name", "") for a in item.get("artists", []) if "name" in a
                ) or "Artiste inconnu"
                thumbnails = item.get("thumbnails", [])
                vignette_url = thumbnails[-1]["url"] if thumbnails else None

                liste_suggestions.append({
                    "titre": titre,
                    "artiste": artistes,
                    "video_id": video_id,
                    "vignette": vignette_url,
                    "lien": f"https://music.youtube.com/watch?v={video_id}",
                    "section": titre_section,
                })

        logger.info("Sections de la page d'accueil récupérées : %s", ", ".join(sections_vues) or "aucune")
        return liste_suggestions

    # =========================================================================
    # PARTIE 2 : TÉLÉCHARGEMENT ET GESTION DU TAMPON
    # =========================================================================

    def _load_history(self):
        """Charge l'historique des morceaux déjà téléchargés (fichier JSON)."""
        if os.path.exists(self.history_path):
            try:
                with open(self.history_path, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Historique illisible, on repart de zéro : %s", e)
        return set()

    def _save_history(self):
        """Sauvegarde l'historique sur disque (best-effort, ne bloque jamais)."""
        try:
            with open(self.history_path, "w", encoding="utf-8") as f:
                json.dump(list(self.history_ids), f)
        except OSError as e:
            logger.warning("Impossible d'écrire l'historique : %s", e)

    def _interruptible_sleep(self, seconds: float) -> None:
        """Attend `seconds`, mais se réveille immédiatement si stop() est appelé entre-temps
        (au lieu de laisser un time.sleep() classique bloquer l'arrêt de l'appli)."""
        self._stop_event.wait(seconds)

    def _call_with_retry(self, func: Callable, *args, **kwargs) -> Any:
        """Appelle func avec retries + backoff exponentiel. Lève après épuisement."""
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            if not self.is_running:
                raise RuntimeError("Arrêt demandé pendant le retry")
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exc = e
                wait = min(2 ** attempt, 30)
                logger.warning(
                    "Tentative %s/%s échouée (%s) — nouvelle tentative dans %ss",
                    attempt, self.max_retries, e, wait,
                )
                self._interruptible_sleep(wait)
        raise last_exc

    def _get_recommendations_generator(self):
        """
        Générateur produisant des morceaux recommandés, en filtrant les
        doublons déjà vus (session courante ou historique persistant).
        Se protège aussi contre les échecs répétés de l'API pour ne pas
        tourner en boucle serrée si le réseau est down.
        """
        consecutive_failures = 0

        while self.is_running:
            try:
                home_sections = self._call_with_retry(self.yt.get_home, limit=5)
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                logger.error("Échec de récupération des recommandations : %s", e)
                # Pause plus longue si l'API échoue de façon répétée,
                # pour éviter de spammer les requêtes.
                self._interruptible_sleep(min(5 * consecutive_failures, 60))
                continue

            any_new = False
            for section in home_sections:
                for item in section.get("contents", []):
                    video_id = item.get("videoId")
                    if not video_id:
                        continue
                    if video_id in self.seen_ids or video_id in self.history_ids:
                        continue
                    self.seen_ids.add(video_id)
                    any_new = True
                    yield item

            if not any_new:
                # Toutes les recos de ce tour étaient déjà vues :
                # on attend un peu avant de refaire un appel API,
                # sinon on boucle à vide en spammant get_home().
                self._interruptible_sleep(10)

    def _enforce_cache_limit(self):
        """Supprime les fichiers les plus anciens du cache si la taille dépasse la limite."""
        try:
            files = [
                os.path.join(self.temp_dir, f)
                for f in os.listdir(self.temp_dir)
                if f.endswith(".mp3")
            ]
        except OSError:
            return

        files.sort(key=lambda p: os.path.getmtime(p))
        total_size = sum(os.path.getsize(p) for p in files)

        while total_size > self.max_cache_bytes and files:
            oldest = files.pop(0)
            try:
                size = os.path.getsize(oldest)
                os.remove(oldest)
                total_size -= size
                logger.info("Cache plein : suppression de %s", os.path.basename(oldest))
            except OSError as e:
                logger.warning("Impossible de supprimer %s : %s", oldest, e)

    def _mark_history(self, video_id: str) -> None:
        """Ajoute un morceau à l'historique et sauvegarde périodiquement sur disque
        (tous les `history_save_every` morceaux), pour ne pas tout perdre en cas de
        fermeture brutale de l'application entre deux arrêts propres."""
        self.history_ids.add(video_id)
        self._downloads_since_save += 1
        if self._downloads_since_save >= self.history_save_every:
            self._save_history()
            self._downloads_since_save = 0

    def _background_loader(self):
        """
        Boucle d'arrière-plan : télécharge les morceaux du générateur et les
        pousse dans ready_queue. S'arrête proprement même si bloquée sur put().
        """
        for item in self.reco_generator:
            if not self.is_running:
                break

            video_id = item["videoId"]
            titre = item.get("title", "Titre inconnu")
            artistes = ", ".join(
                a.get("name", "") for a in item.get("artists", []) if "name" in a
            )
            lien = f"https://music.youtube.com/watch?v={video_id}"

            safe_title = "".join(
                c for c in titre if c.isalpha() or c.isdigit() or c == " "
            ).strip()[:80]  # limite la longueur pour éviter les erreurs de nom de fichier
            filepath = os.path.join(self.temp_dir, f"{safe_title}_{video_id}.mp3")

            if not os.path.exists(filepath):
                try:
                    success = self._download_track(lien, filepath)
                except Exception as e:
                    logger.error("Téléchargement impossible pour %s : %s", lien, e)
                    success = False
                if not success:
                    # On marque quand même comme "vu" pour ne pas boucler
                    # indéfiniment sur un morceau qui échoue systématiquement.
                    self._mark_history(video_id)
                    continue

            self._enforce_cache_limit()

            # put() bloquant classique, mais avec vérification périodique
            # du flag d'arrêt pour ne pas rester bloqué indéfiniment.
            track = {
                "titre": titre,
                "artiste": artistes,
                "lien": lien,
                "video_id": video_id,
                "chemin_local": filepath,
            }
            self._safe_put(track)

            self._mark_history(video_id)

        self._save_history()
        logger.info("Thread de chargement arrêté proprement.")

    def _safe_put(self, track):
        """queue.put() qui se réveille périodiquement pour vérifier is_running."""
        while self.is_running:
            try:
                self.ready_queue.put(track, timeout=1)
                return
            except queue.Full:
                continue

    def _download_track(self, lien, filepath):
        """Télécharge un morceau avec retries. Retourne True/False selon le succès."""
        if shutil.which("ffmpeg") is None:
            logger.error("ffmpeg introuvable dans le PATH : impossible de convertir en MP3.")
            raise RuntimeError(
                "ffmpeg est introuvable. Installe-le (ex: winget install ffmpeg, ou "
                "https://ffmpeg.org/download.html) et vérifie qu'il est bien dans le PATH."
            )

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": filepath,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }

        for attempt in range(1, self.max_retries + 1):
            if not self.is_running:
                return False
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([lien])
                return True
            except Exception as e:
                logger.warning(
                    "Échec du téléchargement (%s/%s) pour %s : %s",
                    attempt, self.max_retries, lien, e,
                )
                # Nettoie un éventuel fichier partiel avant de réessayer
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                    except OSError:
                        pass
                self._interruptible_sleep(min(2 ** attempt, 15))

        logger.error("Abandon du téléchargement après %s tentatives : %s", self.max_retries, lien)
        return False

    def get_next_online_track(self) -> Optional[dict]:
        """Retire et renvoie le prochain morceau disponible dans le tampon."""
        try:
            return self.ready_queue.get_nowait()
        except queue.Empty:
            return None

    def mark_track_played(self, filepath: str) -> None:
        """
        À appeler par le lecteur une fois la lecture d'un morceau terminée,
        pour libérer immédiatement l'espace disque plutôt que d'attendre
        la limite de taille du cache.
        """
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                logger.info("Fichier supprimé après lecture : %s", os.path.basename(filepath))
        except OSError as e:
            logger.warning("Impossible de supprimer %s : %s", filepath, e)

    def stop(self):
        """Arrête proprement le thread et nettoie le cache disque."""
        self.is_running = False
        self._stop_event.set()  # réveille immédiatement tout _interruptible_sleep en cours

        # Vide la queue au cas où le thread serait bloqué sur put() malgré _safe_put
        # (protection supplémentaire, ceinture et bretelles).
        while not self.ready_queue.empty():
            try:
                self.ready_queue.get_nowait()
            except queue.Empty:
                break

        self.loader_thread.join(timeout=5)

        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            logger.info("Cache temporaire nettoyé : %s", self.temp_dir)
        except OSError as e:
            logger.warning("Nettoyage du cache échoué : %s", e)
