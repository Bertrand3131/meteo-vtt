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

## Nature du sol

`sol.json` (argile, sable, cailloux et capacité au champ de la couche 0-5 cm pour chaque point de grille) vient de
SoilGrids 2.0 (ISRIC, licence CC BY 4.0). Il se fabrique une seule fois avec `Preparer le sol.bat` (`preparer_sol.py`,
sans module à installer). La carte l'associe à l'humidité du sol et à l'évapotranspiration d'Open-Meteo pour estimer la boue.
