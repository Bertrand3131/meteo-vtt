# -*- coding: utf-8 -*-
"""
Carte météo VTT
================
Télécharge :
  - la pluie MESURÉE par les stations Météo-France (données quotidiennes
    publiques de data.gouv.fr) sur les 14 derniers jours disponibles ;
  - les PRÉVISIONS de deux modèles (Météo-France et ECMWF) via Open-Meteo,
    sur une grille couvrant la France ;
puis fabrique « Carte meteo VTT.html » et l'ouvre dans le navigateur.

Aucun module à installer : uniquement la bibliothèque standard de Python.
Lancement : double-clic sur « Mettre a jour la carte.bat »
"""

import csv
import datetime as dt
import gzip
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------- réglages
DOSSIER = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(DOSSIER, "cache")
MODELE_HTML = os.path.join(DOSSIER, "modele_carte.html")
SORTIE_HTML = os.environ.get("METEO_SORTIE") or os.path.join(DOSSIER, "Carte meteo VTT.html")
EN_LIGNE = bool(os.environ.get("GITHUB_ACTIONS"))   # exécution sur les serveurs de GitHub

DATASET_QUOT = "6569b51ae64326786e4e8e1a"   # Données climatologiques de base - quotidiennes
API_DATAGOUV = "https://www.data.gouv.fr/api/2/datasets/%s/resources/?page=%d&page_size=100"
JOURS_PLUIE = 14            # jours de pluie passée gardés (la carte en affiche 3/5/10)

# Grille de prévision (degrés). 0.25 x 0.35 ≈ 28 x 28 km.
GRILLE_DLAT = 0.25
GRILLE_DLON = 0.35
GRILLE_MAX_KM_STATION = 30  # on ne garde que les points proches d'une station (= sur terre)
JOURS_PREVISION = 7
JOURS_PASSES_MODELE = 4   # pluie estimée par les modèles sur les derniers jours (comble le retard des stations)
HEURE_DEBUT, HEURE_FIN = 8, 18   # « journée de vélo » : de 8 h à 18 h
LOTS_OPENMETEO = 50              # points par requête
PAUSE_OPENMETEO = 6.5            # secondes entre requêtes (limite gratuite : 600 appels/min, 5 000/h, 10 000/jour)

# Modèles essayés dans l'ordre (le premier accepté par Open-Meteo est gardé)
MODELES = [
    ("Météo-France", ["meteofrance_seamless", "meteofrance_arpege_europe"]),
    ("ECMWF",        ["ecmwf_ifs", "ecmwf_ifs025"]),
]
VARIABLES_HORAIRES = ["precipitation", "wind_speed_10m", "wind_gusts_10m",
                      "sunshine_duration", "temperature_2m", "wind_direction_10m"]

UA = "carte-meteo-vtt/1.0 (usage personnel)"
COPIE_ONEDRIVE = True   # copie aussi la carte dans OneDrive\Meteo VTT pour l'ouvrir sur le téléphone


# ---------------------------------------------------------------- outils
def info(msg):
    print(msg, flush=True)


def http_get(url, entetes=None, timeout=60, essais=3):
    """Renvoie (code, contenu_bytes, entetes_reponse). Réessaie sur coupure réseau,
    puis lève l'exception si ça ne passe toujours pas."""
    for n in range(essais):
        try:
            return _http_get(url, entetes, timeout)
        except (urllib.error.URLError, OSError) as e:
            if n == essais - 1:
                raise
            time.sleep(5 * (n + 1) ** 2)


def _http_get(url, entetes=None, timeout=60):
    h = {"User-Agent": UA}
    if entetes:
        h.update(entetes)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        corps = b""
        try:
            corps = e.read()
        except Exception:
            pass
        return e.code, corps, dict(e.headers or {})


def num(txt):
    if txt is None:
        return None
    t = str(txt).strip().replace(",", ".")
    if t == "":
        return None
    try:
        return float(t)
    except ValueError:
        return None


def dist_km(lat1, lon1, lat2, lon2):
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371 * math.hypot(x, y)


# ---------------------------------------------------------------- 1. pluie mesurée
RE_TITRE = re.compile(r"departement_([0-9AB]{2,3})_periode_(\d{4})-(\d{4})_RR-T-Vent", re.I)


def lister_fichiers_quotidiens():
    """Liste des fichiers « RR-T-Vent » les plus récents, un par département de métropole.
    En cas de panne de data.gouv.fr, reprend la dernière liste connue."""
    chemin = os.path.join(CACHE, "liste_fichiers.json")
    try:
        liste = _lister_fichiers_quotidiens()
        if liste:
            os.makedirs(CACHE, exist_ok=True)
            with open(chemin, "w", encoding="utf-8") as f:
                json.dump(liste, f)
        return liste
    except Exception as e:
        if os.path.exists(chemin):
            info("     data.gouv.fr ne répond pas (%s) : j'utilise la dernière liste connue." % str(e)[:80])
            with open(chemin, encoding="utf-8") as f:
                return json.load(f)
        raise


def _lister_fichiers_quotidiens():
    ressources = []
    page = 1
    while page < 30:
        code, corps, _ = http_get(API_DATAGOUV % (DATASET_QUOT, page))
        if code != 200:
            raise RuntimeError("data.gouv.fr a répondu %s à la liste des fichiers" % code)
        d = json.loads(corps.decode("utf-8"))
        lot = d.get("data") or []
        ressources.extend(lot)
        if not lot or not d.get("next_page"):
            break
        page += 1

    par_dep = {}
    for r in ressources:
        texte = (r.get("title") or "") + " " + (r.get("url") or "")
        m = RE_TITRE.search(texte)
        if not m:
            continue
        dep, fin = m.group(1).upper(), int(m.group(3))
        if len(dep) == 3:            # outre-mer (971, 972…) : ignoré
            continue
        if dep.isdigit() and int(dep) > 95:
            continue
        if dep not in par_dep or fin > par_dep[dep][0]:
            par_dep[dep] = (fin, r.get("url"))
    return {dep: url for dep, (fin, url) in sorted(par_dep.items())}


def telecharger_avec_cache(dep, url):
    """Télécharge un fichier si il a changé depuis la dernière fois (ETag / Last-Modified)."""
    os.makedirs(CACHE, exist_ok=True)
    chemin = os.path.join(CACHE, "Q_%s_RR-T-Vent.csv.gz" % dep)
    meta_chemin = chemin + ".json"
    meta = {}
    if os.path.exists(chemin) and os.path.exists(meta_chemin):
        try:
            with open(meta_chemin, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = {}
    entetes = {}
    if meta.get("url") == url:
        if meta.get("etag"):
            entetes["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            entetes["If-Modified-Since"] = meta["last_modified"]
    try:
        code, corps, rep = http_get(url, entetes, timeout=120)
    except Exception:
        if os.path.exists(chemin):
            return chemin, False      # panne réseau : on garde la version précédente
        raise
    if code == 304:
        return chemin, False
    if code != 200:
        if os.path.exists(chemin):
            return chemin, False      # on garde l'ancienne version
        raise RuntimeError("département %s : réponse %s" % (dep, code))
    with open(chemin, "wb") as f:
        f.write(corps)
    rep_l = {k.lower(): v for k, v in rep.items()}
    with open(meta_chemin, "w", encoding="utf-8") as f:
        json.dump({"url": url, "etag": rep_l.get("etag"),
                   "last_modified": rep_l.get("last-modified")}, f)
    return chemin, True


def lire_pluie(chemin, date_min):
    """Lit un fichier départemental -> {num_poste: {infos, 'rr': {date: mm}}}."""
    stations = {}
    with gzip.open(chemin, "rt", encoding="utf-8", errors="replace", newline="") as f:
        lecteur = csv.reader(f, delimiter=";")
        entete = [c.strip().upper() for c in next(lecteur)]
        i = {nom: entete.index(nom) for nom in
             ("NUM_POSTE", "NOM_USUEL", "LAT", "LON", "ALTI", "AAAAMMJJ", "RR") if nom in entete}
        if len(i) < 7:
            raise RuntimeError("colonnes inattendues dans %s : %s" % (os.path.basename(chemin), entete[:10]))
        for ligne in lecteur:
            if len(ligne) <= max(i.values()):
                continue
            jour = ligne[i["AAAAMMJJ"]].strip()
            if jour < date_min:
                continue
            poste = ligne[i["NUM_POSTE"]].strip()
            s = stations.get(poste)
            if s is None:
                s = stations[poste] = {
                    "nom": ligne[i["NOM_USUEL"]].strip(),
                    "lat": num(ligne[i["LAT"]]), "lon": num(ligne[i["LON"]]),
                    "alt": num(ligne[i["ALTI"]]), "rr": {}}
            rr = num(ligne[i["RR"]])
            if rr is not None:
                s["rr"][jour] = rr
    return stations


def pluie_mesuree():
    info("1/3  Liste des fichiers de stations Météo-France (data.gouv.fr)…")
    fichiers = lister_fichiers_quotidiens()
    if not fichiers:
        raise RuntimeError("aucun fichier « RR-T-Vent » trouvé sur data.gouv.fr")
    info("     %d départements trouvés. Téléchargement (seuls les fichiers modifiés sont rechargés)…" % len(fichiers))

    chemins, nouveaux, erreurs = {}, 0, []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futurs = {ex.submit(telecharger_avec_cache, dep, url): dep for dep, url in fichiers.items()}
        for k, fu in enumerate(as_completed(futurs), 1):
            dep = futurs[fu]
            try:
                chemins[dep], neuf = fu.result()
                nouveaux += neuf
            except Exception as e:
                erreurs.append("%s (%s)" % (dep, e))
            if k % 10 == 0:
                info("     … %d/%d" % (k, len(fichiers)))
    info("     %d fichiers téléchargés, %d inchangés." % (nouveaux, len(chemins) - nouveaux))
    if erreurs:
        info("     Départements en erreur : " + ", ".join(erreurs))

    date_min = (dt.date.today() - dt.timedelta(days=40)).strftime("%Y%m%d")
    toutes = {}
    for dep, chemin in sorted(chemins.items()):
        try:
            for poste, s in lire_pluie(chemin, date_min).items():
                s["dep"] = dep
                toutes[poste] = s
        except Exception as e:
            info("     Lecture impossible pour le %s : %s" % (dep, e))

    # Dernier jour « complet » : le plus récent où au moins la moitié des stations ont une valeur
    compte = {}
    for s in toutes.values():
        for j in s["rr"]:
            compte[j] = compte.get(j, 0) + 1
    if not compte:
        raise RuntimeError("aucune valeur de pluie récente dans les fichiers")
    maxi = max(compte.values())
    dernier = max(j for j, n in compte.items() if n >= 0.5 * maxi)
    # jours récents où seule une partie des stations a déjà publié : gardés aussi (la carte s'en sert
    # là où une station proche a une mesure, et prend le modèle ailleurs)
    partiels = [j for j, n in compte.items() if j > dernier and n >= 0.02 * maxi]
    dernier_partiel = max(partiels) if partiels else dernier
    info("     Stations ayant publié, jour par jour : " +
         ", ".join("%s/%s : %d" % (j[6:], j[4:6], compte.get(j, 0)) for j in sorted(compte)[-6:]))
    d_fin = dt.datetime.strptime(dernier_partiel, "%Y%m%d").date()
    jours = [(d_fin - dt.timedelta(days=JOURS_PLUIE - 1 - k)).strftime("%Y%m%d") for k in range(JOURS_PLUIE)]

    liste = []
    for poste, s in toutes.items():
        if s["lat"] is None or s["lon"] is None:
            continue
        valeurs = [s["rr"].get(j) for j in jours]
        if all(v is None for v in valeurs[-10:]):
            continue        # station sans mesure récente
        liste.append([poste, s["nom"], round(s["lat"], 4), round(s["lon"], 4),
                      None if s["alt"] is None else int(s["alt"]), s["dep"], valeurs])
    info("     %d stations avec des mesures récentes. Dernier jour complet : %s%s." %
         (len(liste), dt.datetime.strptime(dernier, "%Y%m%d").strftime("%d/%m/%Y"),
          "" if dernier_partiel == dernier else ", jours partiels jusqu'au " + d_fin.strftime("%d/%m/%Y")))
    return {"jours": jours, "dernier_complet": dernier, "stations": liste, "erreurs": erreurs}


# ---------------------------------------------------------------- 2. prévisions
URL_OM = "https://api.open-meteo.com/v1/forecast"


def requete_openmeteo(lats, lons, modeles, variables=None):
    params = {
        "latitude": ",".join("%.3f" % x for x in lats),
        "longitude": ",".join("%.3f" % x for x in lons),
        "hourly": ",".join(variables or VARIABLES_HORAIRES),
        "models": ",".join(modeles),
        "timezone": "Europe/Paris",
        "forecast_days": JOURS_PREVISION,
        "past_days": JOURS_PASSES_MODELE,
        "wind_speed_unit": "kmh",
    }
    url = URL_OM + "?" + urllib.parse.urlencode(params, safe=",")
    for essai in range(5):
        try:
            code, corps, _ = http_get(url, timeout=30, essais=1)
        except Exception as e:      # coupure réseau, délai dépassé… : on réessaie
            attente = 5 * (essai + 1)
            info("     Connexion à Open-Meteo interrompue (%s), nouvel essai dans %d s…" % (str(e)[:80], attente))
            time.sleep(attente)
            continue
        if code == 200:
            d = json.loads(corps.decode("utf-8"))
            return d if isinstance(d, list) else [d]
        if code == 429:
            raison = corps[:200].decode("utf-8", "replace")
            info("     Limite d'Open-Meteo atteinte (%s), pause d'une minute…" % raison)
            if "aily" in raison or "ourly" in raison:     # limite horaire ou journalière : inutile d'insister
                raise RuntimeError("limite horaire ou journalière d'Open-Meteo atteinte : %s" % raison)
            time.sleep(65)
            continue
        raise RuntimeError("Open-Meteo a répondu %s : %s" % (code, corps[:300].decode("utf-8", "replace")))
    raise RuntimeError("Open-Meteo ne répond pas ou refuse les requêtes après plusieurs essais. Réessaie plus tard.")


def choisir_modeles():
    choisis = []
    for nom, candidats in MODELES:
        for c in candidats:
            try:
                requete_openmeteo([45.0], [3.0], [c])
                choisis.append((nom, c))
                break
            except RuntimeError as e:
                info("     Modèle %s refusé (%s), j'essaie le suivant." % (c, str(e)[:80]))
        else:
            info("     Aucun modèle disponible pour %s." % nom)
    if not choisis:
        raise RuntimeError("aucun modèle de prévision accepté par Open-Meteo")
    return choisis


def grille(stations):
    """Points de grille sur la France. On prend la grille fixe « grille.json » si elle existe
    (toujours la même, même si les stations manquent), sinon les points proches d'une station."""
    fixe = os.path.join(DOSSIER, "grille.json")
    if os.path.exists(fixe):
        try:
            with open(fixe, encoding="utf-8") as f:
                g = json.load(f)
            if abs(g.get("dlat", 0) - GRILLE_DLAT) < 1e-9 and abs(g.get("dlon", 0) - GRILLE_DLON) < 1e-9:
                return [tuple(p) for p in g["points"]]
        except Exception as e:
            info("     grille.json illisible (%s), je recalcule la grille." % e)
    seau = {}
    for s in stations:
        seau.setdefault((int(s[2] * 4), int(s[3] * 4)), []).append((s[2], s[3]))
    pts = []
    lat = 41.30
    while lat <= 51.15:
        lon = -5.20
        while lon <= 9.65:
            proche = False
            cle = (int(lat * 4), int(lon * 4))
            for di in (-2, -1, 0, 1, 2):
                for dj in (-2, -1, 0, 1, 2):
                    for (a, b) in seau.get((cle[0] + di, cle[1] + dj), ()):
                        if dist_km(lat, lon, a, b) <= GRILLE_MAX_KM_STATION:
                            proche = True
                            break
                    if proche:
                        break
                if proche:
                    break
            if proche:
                pts.append((round(lat, 3), round(lon, 3)))
            lon += GRILLE_DLON
        lat += GRILLE_DLAT
    return pts


def resumer_jour(heures, series, jour):
    """Valeurs du jour : [pluie 8-18h, pluie 24h, vent max 8-18h, rafales max, soleil (h), t max,
    direction du vent (d'où il vient, en degrés) moyenne pondérée par la vitesse entre 8 h et 18 h]."""
    idx_jour = [k for k, h in enumerate(heures) if h.startswith(jour)]
    idx_velo = [k for k in idx_jour if HEURE_DEBUT <= int(heures[k][11:13]) < HEURE_FIN]

    def vals(nom, idx):
        s = series.get(nom) or []
        return [s[k] for k in idx if k < len(s) and s[k] is not None]

    def somme(v, min_n):
        return round(sum(v), 1) if len(v) >= min_n else None

    def maxi(v):
        return round(max(v)) if v else None

    p_velo = somme(vals("precipitation", idx_velo), len(idx_velo) * 0.8)
    p_24 = somme(vals("precipitation", idx_jour), len(idx_jour) * 0.8)
    vent = maxi(vals("wind_speed_10m", idx_velo))
    raf = maxi(vals("wind_gusts_10m", idx_velo))
    sol = vals("sunshine_duration", idx_velo)
    soleil = round(sum(sol) / 3600, 1) if len(sol) >= len(idx_velo) * 0.8 and idx_velo else None
    tmax = vals("temperature_2m", idx_velo)
    tmax = round(max(tmax)) if tmax else None
    # direction moyenne (vecteur) pondérée par la vitesse
    sx = sy = 0.0
    sv = series.get("wind_speed_10m") or []
    sd = series.get("wind_direction_10m") or []
    for k in idx_velo:
        if k < len(sv) and k < len(sd) and sv[k] is not None and sd[k] is not None:
            sx += sv[k] * math.sin(math.radians(sd[k]))
            sy += sv[k] * math.cos(math.radians(sd[k]))
    direction = round(math.degrees(math.atan2(sx, sy)) % 360) if (sx or sy) else None
    return [p_velo, p_24, vent, raf, soleil, tmax, direction]


VARIABLES_SOL = ["et0_fao_evapotranspiration", "soil_moisture_0_to_7cm"]


def charger_sol():
    """Nature du sol par point (fichier sol.json fabriqué une fois par preparer_sol.py)."""
    chemin = os.path.join(DOSSIER, "sol.json")
    if not os.path.exists(chemin):
        info("     (sol.json absent : la nature du sol ne sera pas prise en compte. Voir « Preparer le sol.bat ».)")
        return {}
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f).get("points", {})
    except Exception as e:
        info("     sol.json illisible : %s" % e)
        return {}


def prevoir_points(pts, codes, codes_tous, jours, jours_passes, avec_direction=True, sol=None, avec_humidite=True):
    """Prévisions pour une liste de points avec les modèles « codes ».
    Les résultats sont rangés dans l'ordre de « codes_tous » (None pour un modèle non demandé)."""
    resultat = []
    for debut in range(0, len(pts), LOTS_OPENMETEO):
        lot = pts[debut:debut + LOTS_OPENMETEO]
        if debut:
            time.sleep(PAUSE_OPENMETEO)
        variables = VARIABLES_HORAIRES + (VARIABLES_SOL if avec_humidite else VARIABLES_SOL[:1])
        reps = requete_openmeteo([p[0] for p in lot], [p[1] for p in lot], codes, variables)
        for (lat, lon), rep in zip(lot, reps):
            h = rep.get("hourly") or {}
            heures = h.get("time") or []
            index_h = {t: k for k, t in enumerate(heures)}

            def serie_de(nom, code):
                v = h.get(nom + "_" + code)
                if v is None and len(codes) == 1:
                    v = h.get(nom)
                return v

            def horaire(nom, dec):
                res = []
                for code in codes_tous:
                    if code not in codes:
                        res.append(None)
                        continue
                    serie = serie_de(nom, code) or []
                    vals = []
                    for jour in jours:
                        for hh in range(24):
                            k = index_h.get("%sT%02d:00" % (jour, hh))
                            v = serie[k] if k is not None and k < len(serie) else None
                            vals.append(None if v is None else (round(v, dec) if dec else int(round(v))))
                    res.append(vals)
                return res

            par_jour = []
            for jour in jours:
                ligne = []
                for code in codes_tous:
                    if code not in codes:
                        ligne.append(None)
                        continue
                    series = {v: serie_de(v, code) for v in VARIABLES_HORAIRES}
                    ligne.append(resumer_jour(heures, series, jour))
                par_jour.append(ligne)
            passes = [[resumer_jour(heures, {"precipitation": serie_de("precipitation", c)}, j)[1] for j in jours_passes]
                      if c in codes else None for c in codes_tous]
            resultat.append([lat, lon, par_jour, horaire("precipitation", 1),
                             horaire("wind_speed_10m", 0), horaire("wind_gusts_10m", 0),
                             horaire("wind_direction_10m", 0) if avec_direction else None, passes,
                             horaire("temperature_2m", 0),
                             # [9] nature du sol : argile %, sable %, cailloux %, capacité au champ %
                             (sol or {}).get("%.3f,%.3f" % (lat, lon)),
                             # [10] humidité du sol 0-7 cm (m³/m³), heure par heure
                             horaire("soil_moisture_0_to_7cm", 2) if avec_humidite else None,
                             # [11] évapotranspiration de référence (mm/jour) : jours passés puis jours prévus
                             [[round(sum(v for v in (serie_de("et0_fao_evapotranspiration", c) or [])[k0:k0 + 24] if v is not None), 2)
                               if index_h.get(j + "T00:00") is not None and (serie_de("et0_fao_evapotranspiration", c) or []) else None
                               for j in jours_passes + jours
                               for k0 in [index_h.get(j + "T00:00", 0)]]
                              if c in codes else None for c in codes_tous]])
        info("     … %d/%d points" % (min(debut + LOTS_OPENMETEO, len(pts)), len(pts)))
    return resultat


def previsions(stations):
    info("2/3  Prévisions Open-Meteo…")
    modeles = choisir_modeles()
    info("     Modèles utilisés : " + ", ".join("%s (%s)" % m for m in modeles))
    pts = grille(stations)
    info("     %d points de grille sur la France, par lots de %d." % (len(pts), LOTS_OPENMETEO))

    aujourd_hui = dt.date.today()
    jours = [(aujourd_hui + dt.timedelta(days=k)).isoformat() for k in range(JOURS_PREVISION)]
    jours_passes = [(aujourd_hui - dt.timedelta(days=JOURS_PASSES_MODELE - k)).isoformat() for k in range(JOURS_PASSES_MODELE)]
    codes = [m[1] for m in modeles]
    sol = charger_sol()
    if sol:
        info("     Nature du sol connue pour %d points." % len(sol))
    resultat = prevoir_points(pts, codes, codes, jours, jours_passes, sol=sol)
    sortie = {"jours": jours, "jours_passes": jours_passes, "modeles": [m[0] for m in modeles], "codes": codes,
              "dlat": GRILLE_DLAT, "dlon": GRILLE_DLON, "lat0": 41.30, "lon0": -5.20, "points": resultat,
              "heures_velo": [HEURE_DEBUT, HEURE_FIN]}

    # grille fine (environ 9 km) sur la zone de « grille_fine.json », modèle Météo-France seulement
    fichier = os.path.join(DOSSIER, "grille_fine.json")
    code_mf = next((c for n, c in modeles if n == "Météo-France"), None)
    if os.path.exists(fichier) and code_mf:
        try:
            with open(fichier, encoding="utf-8") as f:
                gf = json.load(f)
            pf = [tuple(p) for p in gf["points"]]
            info("     Grille fine (%s) : %d points, modèle Météo-France…" % (gf.get("zone", ""), len(pf)))
            sortie["fin"] = {"zone": gf.get("zone", ""), "lat0": gf["lat0"], "lon0": gf["lon0"],
                             "dlat": gf["dlat"], "dlon": gf["dlon"],
                             "points": prevoir_points(pf, [code_mf], codes, jours, jours_passes, avec_direction=False,
                                                           sol=sol, avec_humidite=False)}
        except Exception as e:
            info("     Grille fine indisponible : %s (la carte garde la grille normale)" % e)
    return sortie


# ---------------------------------------------------------------- 3. carte
def ecrire_carte(donnees):
    info("3/3  Fabrication de la carte…")
    with open(MODELE_HTML, encoding="utf-8") as f:
        modele = f.read()
    js = json.dumps(donnees, ensure_ascii=False, separators=(",", ":"))
    html = modele.replace("/*__DONNEES__*/null", js)
    os.makedirs(os.path.dirname(os.path.abspath(SORTIE_HTML)), exist_ok=True)
    tmp = SORTIE_HTML + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, SORTIE_HTML)
    # petit fichier « version » : la carte ouverte le consulte pour se recharger toute seule
    with open(os.path.join(os.path.dirname(os.path.abspath(SORTIE_HTML)), "version.json"), "w", encoding="utf-8") as f:
        json.dump({"genere": donnees["genere"]}, f)
    info("     Carte écrite : %s (%.1f Mo)" % (SORTIE_HTML, os.path.getsize(SORTIE_HTML) / 1e6))
    # copie pour le téléphone dans OneDrive (si OneDrive est installé sur ce PC)
    if COPIE_ONEDRIVE and not EN_LIGNE:
        onedrive = os.environ.get("OneDrive") or os.path.join(os.path.expanduser("~"), "OneDrive")
        if os.path.isdir(onedrive):
            try:
                dossier = os.path.join(onedrive, "Meteo VTT")
                os.makedirs(dossier, exist_ok=True)
                import shutil
                shutil.copyfile(SORTIE_HTML, os.path.join(dossier, "Carte meteo VTT.html"))
                info("     Copie pour le téléphone : %s" % dossier)
            except Exception as e:
                info("     Copie OneDrive impossible : %s" % e)


def main():
    debut = time.time()
    donnees = {"genere": dt.datetime.now().strftime("%Y-%m-%dT%H:%M"), "pluie": None,
               "prevision": None, "alertes": []}
    try:
        donnees["pluie"] = pluie_mesuree()
    except Exception as e:
        info("     ERREUR pluie mesurée : %s" % e)
        donnees["alertes"].append("Pluie mesurée indisponible : %s" % e)
    stations = (donnees["pluie"] or {}).get("stations") or []
    if not stations:
        # Sans stations, on prend une grille grossière sur tout le rectangle France
        stations = [[None, None, 41.4 + 0.5 * i, -5.0 + 0.5 * j] for i in range(20) for j in range(30)]
    try:
        donnees["prevision"] = previsions(stations)
    except Exception as e:
        info("     ERREUR prévisions : %s" % e)
        donnees["alertes"].append("Prévisions indisponibles : %s" % e)
    ecrire_carte(donnees)
    info("Terminé en %d s." % (time.time() - debut))
    if "--sans-ouvrir" not in sys.argv and not EN_LIGNE:
        webbrowser.open("file:///" + SORTIE_HTML.replace("\\", "/"))
    return 0 if not donnees["alertes"] else 1


if __name__ == "__main__":
    sys.exit(main())
