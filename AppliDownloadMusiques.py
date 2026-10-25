# -*- coding: utf-8 -*-
import os
import yt_dlp

"""
Dans lien.txt, entre tout tes liens youtube. Si c'est une playlist, ATTENTION,
toute la playlist sera téléchargée
"""

FICHIER_LIENS = "lien.txt"

def charger_liens(fichier):
    if not os.path.exists(fichier):
        print(f"Erreur : Le fichier '{fichier}' est introuvable.")
        return []
    with open(fichier, "r", encoding="utf-8") as f:
        return [ligne.strip() for ligne in f if ligne.strip()]

def main():
    urls = charger_liens(FICHIER_LIENS)
    if not urls:
        print("Aucun lien à télécharger. Fin du programme.")
        return

    print(f"--- {len(urls)} lien(s) détecté(s) dans {FICHIER_LIENS} ---")
    
    choix = str(input("Voulez-vous télécharger en MP4 ? (oui/non) [Défaut = MP3] : ").strip().lower())
    telecharger_mp4 = choix in ['o', 'oui', 'y', 'yes']

    # --- OPTIONS COMMUNES ---
    options_communes = {
        # Playlist -> sous-dossier "Nom de la playlist/" / Vidéo solo -> sous-dossier "NA/"
        'outtmpl': '%(playlist_title|NA)s/%(title)s.%(ext)s',
        'ignoreerrors': True,
        'quiet': False,
        'no_warnings': False,
        'skip_unavailable_fragments': True,
        'noplaylist': False,

        # ios/android/web : combinaison qui fonctionne pour contenu public YouTube.
        # Les warnings SABR sur ios/android sont sans consequence : les flux M4A restent disponibles.
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'android', 'web']
            }
        }
    }

    # --- APPLICATION DES OPTIONS ---
    if not telecharger_mp4:
        print("\n-> Mode Audio (MP3) sélectionné.")
        ydl_opts = {
            **options_communes,
            # Priorité : flux audio-only M4A -> Opus/WebM -> tout audio -> meilleur format dispo
            # Evite le fallback sur un flux video+audio MP4 quand seul l'audio est voulu
            'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
            'postprocessors': [
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '320',  # Qualite maximale MP3
                },
                {
                    'key': 'FFmpegMetadata',
                }
            ]
        }
    else:
        print("\n-> Mode Vidéo (MP4) sélectionné.")
        ydl_opts = {
            **options_communes, 
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'merge_output_format': 'mp4',
            'postprocessors': [
                {
                    'key': 'FFmpegMetadata',
                }
            ]
        }

    # --- LANCEMENT DU TELECHARGEMENT ---
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for i, url in enumerate(urls, start=1):
            print(f"\n==========================================")
            print(f"Téléchargement {i}/{len(urls)} : {url}")
            print(f"==========================================")
            try:
                ydl.download([url])
            except Exception as e:
                print(f"Erreur critique sur l'URL {url} : {e}")

    print("\nTous les téléchargements applicables sont terminés !")

if __name__ == "__main__":
    main()