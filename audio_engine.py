"""
audio_engine.py
================
Couche d'abstraction audio : isole tous les appels au moteur de lecture (pygame.mixer ou VLC)
derrière une interface commune (AudioEngine), pour que MusicPlayer n'ait plus jamais à appeler
pygame ou vlc directement. Objectif : permettre de basculer vers python-vlc (streaming/lecture
progressive, seek fiable, fin de piste par événement natif au lieu d'un polling de position)
sans toucher au reste de l'application, et sans risque de régression si VLC n'est pas installé.

Utilisation dans MusicPlayer.__init__ :
    from audio_engine import create_audio_engine
    self.audio = create_audio_engine(self.root, prefer_vlc=True)
    self.audio.set_end_callback(self.play_next)   # appelé sur le thread principal Tkinter

Toutes les positions/durées manipulées par cette interface sont en SECONDES (float), jamais en
millisecondes -- chaque implémentation convertit en interne si son moteur sous-jacent utilise
une autre unité (VLC travaille nativement en ms, pygame en ms aussi pour get_pos()).
"""

import logging

logger = logging.getLogger("audio_engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class AudioEngine:
    """Interface commune. Toute nouvelle implémentation doit respecter cette interface (secondes,
    volume 0.0-1.0) pour rester interchangeable avec MusicPlayer sans modification de son code."""

    def __init__(self, root):
        self.root = root
        self._end_callback = None

    def set_end_callback(self, callback):
        """`callback` est appelé (sur le thread principal Tkinter, sans argument) quand la piste
        en cours se termine NATURELLEMENT. Ne doit jamais être déclenché pour un stop() explicite
        ou un seek -- c'est la responsabilité de chaque implémentation de garantir ça."""
        self._end_callback = callback

    def load(self, path):
        """Charge un fichier local. Ne démarre pas la lecture (voir play())."""
        raise NotImplementedError

    def play(self, start=0.0):
        """Démarre/reprend la lecture depuis `start` secondes du fichier chargé."""
        raise NotImplementedError

    def pause(self):
        raise NotImplementedError

    def unpause(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def is_busy(self) -> bool:
        """True si un morceau est activement en train de jouer (pas en pause, pas arrêté)."""
        raise NotImplementedError

    def get_position(self) -> float:
        """Position actuelle de lecture en secondes, ou -1 si inconnue/non applicable."""
        raise NotImplementedError

    def set_volume(self, value: float):
        """`value` entre 0.0 et 1.0."""
        raise NotImplementedError

    def rewind(self):
        """Revient au tout début du morceau actuellement chargé, sans le recharger."""
        raise NotImplementedError

    def set_track_length(self, seconds: float):
        """Informe le moteur de la durée totale du morceau (métadonnée déjà connue par
        MusicPlayer via TinyTag). Sans effet pour les moteurs qui déterminent la durée
        eux-mêmes (VLC) ; nécessaire pour la détection de fin par polling (pygame)."""
        pass

    def begin_seek(self):
        """Signale le début d'un glissé sur la barre de progression. No-op par défaut -- seul
        pygame en a besoin (pour ignorer son polling interne pendant le rechargement du fichier,
        qui provoque un `get_busy() == False` transitoire à ne pas confondre avec une fin réelle)."""
        pass

    def end_seek(self):
        """Signale la fin d'un glissé. No-op par défaut."""
        pass

    def shutdown(self):
        """Arrêt propre (appelé à la fermeture de l'application)."""
        raise NotImplementedError


class PygameAudioEngine(AudioEngine):
    """Implémentation de repli, basée sur pygame.mixer.music -- comportement strictement
    identique à l'ancien code de MusicPlayer, simplement déplacé ici. La détection de fin de
    piste par polling (auparavant check_music_end()/update_timer() dans MusicPlayer) est
    encapsulée ici : elle repose sur la dernière position connue PENDANT la lecture, jamais sur
    un get_pos() relu après coup (pygame retourne -1 une fois arrêté -- bug déjà rencontré et
    corrigé, voir README)."""

    def __init__(self, root):
        super().__init__(root)
        import pygame
        self._pygame = pygame
        pygame.mixer.init()

        self._current_path = None
        self._track_length = 0.0
        self._seek_offset = 0.0
        self._last_known_position = 0.0
        self._paused = False
        self._stopped = True
        self._is_sliding = False

        self._poll()  # démarre la boucle de polling (fin de piste + position)

    def _poll(self):
        pg = self._pygame
        if not self._is_sliding:
            if pg.mixer.music.get_busy() and not self._paused:
                self._last_known_position = self._seek_offset + (pg.mixer.music.get_pos() / 1000)
            elif not pg.mixer.music.get_busy() and not self._paused and not self._stopped:
                if self._track_length > 0 and self._last_known_position >= (self._track_length - 1.5):
                    self._stopped = True
                    if self._end_callback:
                        self._end_callback()
        self.root.after(500, self._poll)

    def load(self, path):
        self._current_path = path
        self._pygame.mixer.music.load(path)
        self._seek_offset = 0.0
        self._last_known_position = 0.0

    def play(self, start=0.0):
        # Recharger avant de (re)jouer est nécessaire avec pygame pour un seek fiable
        # (comportement documenté/historique, conservé tel quel).
        if self._current_path:
            self._pygame.mixer.music.load(self._current_path)
        self._pygame.mixer.music.play(start=start)
        self._seek_offset = start
        self._last_known_position = start
        self._paused = False
        self._stopped = False

    def pause(self):
        self._pygame.mixer.music.pause()
        self._paused = True

    def unpause(self):
        self._pygame.mixer.music.unpause()
        self._paused = False

    def stop(self):
        self._pygame.mixer.music.stop()
        self._stopped = True
        self._paused = False

    def is_busy(self):
        return bool(self._pygame.mixer.music.get_busy()) and not self._stopped and not self._paused

    def get_position(self):
        return self._last_known_position

    def set_volume(self, value):
        self._pygame.mixer.music.set_volume(value)

    def rewind(self):
        self._pygame.mixer.music.rewind()
        self._seek_offset = 0.0
        self._last_known_position = 0.0

    def set_track_length(self, seconds):
        self._track_length = seconds or 0.0

    def begin_seek(self):
        self._is_sliding = True

    def end_seek(self):
        self._is_sliding = False

    def shutdown(self):
        try:
            self._pygame.mixer.music.stop()
            self._pygame.mixer.quit()
        except Exception:
            pass


class VlcAudioEngine(AudioEngine):
    """Implémentation VLC (libVLC via python-vlc). Avantages sur pygame, dans l'ordre où ils
    répondent aux limites déjà rencontrées avec pygame dans ce projet :
      - Lecture progressive/streaming : n'exige pas un fichier local entièrement écrit sur le
        disque pour démarrer (pertinent pour le futur, ex. lecture directe depuis le tampon
        YouTube pendant son téléchargement -- pas encore exploité ici, voir README).
      - Fin de piste par ÉVÉNEMENT natif (MediaPlayerEndReached), donc plus besoin du polling de
        position ni du hack _last_known_position propre à pygame.
      - Seek fiable sans recharger le fichier (donc aucun risque du bug historique où un seek
        pouvait être pris pour une fin de piste)."""

    def __init__(self, root):
        super().__init__(root)
        import vlc
        self._vlc = vlc
        self._instance = vlc.Instance("--quiet")
        self._player = self._instance.media_player_new()
        self._media = None
        self._current_path = None

        events = self._player.event_manager()
        # ATTENTION : cet événement est déclenché depuis un thread interne VLC, jamais le thread
        # Tkinter principal -- on ne fait donc QUE replanifier via root.after (thread-safe),
        # sans jamais toucher un widget directement dans _on_vlc_end.
        events.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_vlc_end)

    def _on_vlc_end(self, event):
        self.root.after(0, self._handle_end_on_main_thread)

    def _handle_end_on_main_thread(self):
        if self._end_callback:
            self._end_callback()

    def load(self, path):
        self._current_path = path
        self._media = self._instance.media_new(path)
        self._player.set_media(self._media)

    def play(self, start=0.0):
        if start and start > 0:
            # Option média plutôt qu'un set_time() juste après play() : plus fiable, évite une
            # course où le flux n'est pas encore prêt à accepter un seek au moment du set_time().
            self._media = self._instance.media_new(self._current_path)
            self._media.add_option(f":start-time={start:.2f}")
            self._player.set_media(self._media)
        self._player.play()

    def pause(self):
        self._player.set_pause(1)

    def unpause(self):
        self._player.set_pause(0)

    def stop(self):
        self._player.stop()

    def is_busy(self):
        return bool(self._player.is_playing())

    def get_position(self):
        ms = self._player.get_time()
        return (ms / 1000) if ms is not None and ms >= 0 else -1

    def set_volume(self, value):
        self._player.audio_set_volume(int(max(0, min(100, round(value * 100)))))

    def rewind(self):
        self._player.set_time(0)

    def shutdown(self):
        try:
            self._player.stop()
            self._player.release()
            self._instance.release()
        except Exception:
            pass


def create_audio_engine(root, prefer_vlc=True):
    """Essaie VLC en priorité (voir avantages ci-dessus), et retombe automatiquement sur pygame
    si python-vlc n'est pas installé (pip) OU si la librairie système libVLC est absente (VLC
    doit être installé sur la machine, pas seulement le paquet Python) -- l'application doit
    rester utilisable sans configuration supplémentaire côté utilisateur."""
    if prefer_vlc:
        try:
            engine = VlcAudioEngine(root)
            logger.info("Moteur audio actif : VLC")
            return engine
        except Exception as e:
            logger.warning("VLC indisponible (%s) -- repli sur pygame.", e)
    engine = PygameAudioEngine(root)
    logger.info("Moteur audio actif : pygame")
    return engine
