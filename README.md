# Météo VTT

Carte météo personnelle pour choisir où aller rouler en VTT :

- **pluie tombée** sur 3, 5 ou 10 jours (stations Météo-France, données publiques de data.gouv.fr) ;
- **prévisions** heure par heure (pluie, vent, rafales) et par jour (soleil, température), modèles Météo-France et ECMWF via [Open-Meteo](https://open-meteo.com/) ;
- **« Où rouler ? »** : classement relatif des endroits selon la boue, la pluie et le vent pendant la sortie, limitable à une zone atteignable en voiture.

La carte est reconstruite automatiquement plusieurs fois par jour par GitHub Actions
(`.github/workflows/mise-a-jour.yml`) et publiée sur GitHub Pages.

Fichiers :
- `meteo_vtt.py` : télécharge les données et fabrique la carte (Python, bibliothèque standard uniquement) ;
- `modele_carte.html` : la page de la carte (Leaflet) ;
- `web/` : icônes, manifeste et service worker pour installer la carte comme une appli sur téléphone.

Données : © Météo-France (licence ouverte), Open-Meteo (CC BY 4.0), fonds de carte Esri, OpenTopoMap, CyclOSM, IGN, © contributeurs OpenStreetMap.
