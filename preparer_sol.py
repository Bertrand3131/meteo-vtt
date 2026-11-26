# -*- coding: utf-8 -*-
"""
Préparation de la nature du sol (à lancer une seule fois)
==========================================================
Pour chaque point des grilles de la carte (grille.json et grille_fine.json), relève dans
SoilGrids 2.0 (ISRIC, 250 m, licence CC BY 4.0) la composition de la couche de surface (0-5 cm) :
argile, sable, cailloux, carbone organique. En déduit l'eau que ce sol retient normalement
(« capacité au champ », formule de Saxton et Rawls 2006). Écrit le tout dans sol.json.

Aucun module à installer : les cartes sont demandées par morceaux au service WCS de l'ISRIC
(maps.isric.org) et lues directement par ce programme.
"""
import json
import math
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor

DOSSIER = os.path.dirname(os.path.abspath(__file__))
SORTIE = os.path.join(DOSSIER, "sol.json")
CACHE = os.path.join(DOSSIER, "cache", "sol")
WCS = "https://maps.isric.org/mapserv?map=/map/%s.map"
# propriété : (service, couverture, facteur pour obtenir des %)
PROPRIETES = {
    "argile":   ("clay", "clay_0-5cm_mean", 10.0),    # g/kg -> %
    "sable":    ("sand", "sand_0-5cm_mean", 10.0),    # g/kg -> %
    "cailloux": ("cfvo", "cfvo_0-5cm_mean", 10.0),    # cm3/dm3 -> % du volume
    "carbone":  ("soc", "soc_0-5cm_mean", 100.0),     # dg/kg -> %
}
TUILE = 100000          # morceaux de 100 km × 100 km (400 × 400 pixels de 250 m)
# autour de chaque point, on moyenne 5 relevés (centre + 4 à environ 1 km) pour éviter
# de tomber sur une ville, une rivière ou un champ atypique
DECALAGES = [(0, 0), (1000, 0), (-1000, 0), (0, 1000), (0, -1000)]


def info(msg):
    print(msg, flush=True)


# ------------------------------------------------------------------ projection des cartes SoilGrids
R = 6378137.0


def homolosine(lon, lat):
    """Projection de Goode (homolosine interrompue) des cartes SoilGrids, partie Mollweide
    (latitudes > 40°44' : toute la France). Vérifiée contre PROJ « +proj=igh »."""
    lo0 = -100.0 if lon < -40 else 30.0
    phi = math.radians(lat)
    k = math.pi * math.sin(phi)
    t = phi
    for _ in range(40):
        t -= (2 * t + math.sin(2 * t) - k) / (2 + 2 * math.cos(2 * t))
    x = R * (2 * math.sqrt(2) / math.pi * math.radians(lon - lo0) * math.cos(t)) + R * math.radians(lo0)
    y = R * (math.sqrt(2) * math.sin(t) - 0.0528035274542)
    return x, y


# ------------------------------------------------------------------ lecture d'un GeoTIFF (sans module)
def _lzw(data):
    """Décompression LZW des TIFF."""
    out = bytearray()
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    bits, pos, n = 9, 0, len(data) * 8
    prec = None
    while pos + bits <= n:
        octet = pos >> 3
        morceau_ = int.from_bytes(data[octet:octet + 4].ljust(4, b"\0"), "big")
        code = (morceau_ >> (32 - (pos & 7) - bits)) & ((1 << bits) - 1)
        pos += bits
        if code == 256:
            table = table[:258]
            bits, prec = 9, None
            continue
        if code == 257:
            break
        if code < len(table):
            entree = table[code]
            if prec is not None:
                table.append(prec + entree[:1])
        else:
            entree = prec + prec[:1]
            table.append(entree)
        out += entree
        prec = entree
        if len(table) + 1 >= (1 << bits) and bits < 12:
            bits += 1
    return bytes(out)


def lire_tiff(octets):
    """Renvoie (largeur, hauteur, liste de valeurs, nodata). Gère bandes ou tuiles,
    sans compression, deflate ou LZW, avec ou sans prédicteur horizontal."""
    bo = {b"II": "<", b"MM": ">"}[octets[:2]]
    if struct.unpack(bo + "H", octets[2:4])[0] != 42:
        raise ValueError("TIFF non reconnu (BigTIFF ?)")
    ifd = struct.unpack(bo + "I", octets[4:8])[0]
    n = struct.unpack(bo + "H", octets[ifd:ifd + 2])[0]
    tailles = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 16: 8}
    formats = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 11: "f", 12: "d", 16: "Q"}
    tags = {}
    for i in range(n):
        e = ifd + 2 + 12 * i
        tag, typ, cnt = struct.unpack(bo + "HHI", octets[e:e + 8])
        taille = tailles.get(typ, 1) * cnt
        off = e + 8 if taille <= 4 else struct.unpack(bo + "I", octets[e + 8:e + 12])[0]
        if typ == 2:
            tags[tag] = octets[off:off + cnt].rstrip(b"\0").decode("latin-1")
        elif typ in formats:
            tags[tag] = list(struct.unpack(bo + formats[typ] * cnt, octets[off:off + taille]))
    W, H = tags[256][0], tags[257][0]
    bps = tags.get(258, [16])[0]
    fmt = tags.get(339, [2 if bps == 16 else 1])[0]
    comp = tags.get(259, [1])[0]
    pred = tags.get(317, [1])[0]
    if tags.get(277, [1])[0] != 1:
        raise ValueError("plusieurs bandes")
    code = {(16, 2): "h", (16, 1): "H", (32, 3): "f", (32, 2): "i", (8, 1): "B", (8, 2): "b"}[(bps, fmt)]
    nb = bps // 8
    nodata = None
    if 42113 in tags:
        try:
            nodata = float(tags[42113])
        except ValueError:
            pass

    def decomp(b):
        if comp == 1:
            return b
        if comp in (8, 32946):
            return zlib.decompress(b)
        if comp == 5:
            return _lzw(b)
        raise ValueError("compression TIFF %d non gérée" % comp)

    def valeurs(b, largeur):
        v = list(struct.unpack(bo + code * (len(b) // nb), b[:len(b) // nb * nb]))
        if pred == 2:
            for k in range(len(v)):
                if k % largeur:
                    v[k] = v[k] + v[k - 1]
                    if code == "h":
                        v[k] = (v[k] + 32768) % 65536 - 32768
        return v

    grille = [None] * (W * H)
    if 322 in tags:   # tuiles
        tw, th = tags[322][0], tags[323][0]
        offs, cnts = tags[324], tags[325]
        par_ligne = (W + tw - 1) // tw
        for t, (o, c) in enumerate(zip(offs, cnts)):
            v = valeurs(decomp(octets[o:o + c]), tw)
            ti, tj = divmod(t, par_ligne)
            for r in range(th):
                y = ti * th + r
                if y >= H:
                    break
                for cc in range(tw):
                    x = tj * tw + cc
                    if x < W and r * tw + cc < len(v):
                        grille[y * W + x] = v[r * tw + cc]
    else:             # bandes
        offs, cnts = tags[273], tags[279]
        rps = tags.get(278, [H])[0]
        for s, (o, c) in enumerate(zip(offs, cnts)):
            v = valeurs(decomp(octets[o:o + c]), W)
            debut = s * rps * W
            fin = min(W * H, debut + len(v))
            grille[debut:fin] = v[:fin - debut]
    return W, H, grille, nodata


# ------------------------------------------------------------------ téléchargement des morceaux
def url_morceau(service, couverture, x0, y0, version):
    if version == "2.0.1":
        q = [("SERVICE", "WCS"), ("VERSION", "2.0.1"), ("REQUEST", "GetCoverage"),
             ("COVERAGEID", couverture), ("FORMAT", "image/tiff"),
             ("SUBSET", "X(%d,%d)" % (x0, x0 + TUILE)), ("SUBSET", "Y(%d,%d)" % (y0, y0 + TUILE))]
    elif version == "2.0.1i":
        q = [("SERVICE", "WCS"), ("VERSION", "2.0.1"), ("REQUEST", "GetCoverage"),
             ("COVERAGEID", couverture), ("FORMAT", "GEOTIFF_INT16"),
             ("SUBSET", "X(%d,%d)" % (x0, x0 + TUILE)), ("SUBSET", "Y(%d,%d)" % (y0, y0 + TUILE))]
    else:
        q = [("SERVICE", "WCS"), ("VERSION", "1.0.0"), ("REQUEST", "GetCoverage"),
             ("COVERAGE", couverture), ("FORMAT", "GEOTIFF_INT16"), ("CRS", "EPSG:152160"),
             ("BBOX", "%d,%d,%d,%d" % (x0, y0, x0 + TUILE, y0 + TUILE)), ("RESX", "250"), ("RESY", "250")]
    return WCS % service + "&" + urllib.parse.urlencode(q, safe="(),:/")


def telecharger(url, essais=4):
    derniere = None
    for k in range(essais):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "meteo-vtt (carte personnelle)"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            corps = e.read()[:400].decode("utf-8", "replace")
            derniere = "HTTP %d : %s" % (e.code, corps)
            if e.code in (400, 404):
                break
        except Exception as e:
            derniere = str(e)
        time.sleep(3 * (k + 1))
    raise IOError(derniere)


VERSION_OK = None


def morceau(service, couverture, x0, y0):
    """Renvoie (W, H, valeurs, nodata) pour un morceau, en le gardant en cache."""
    global VERSION_OK
    os.makedirs(CACHE, exist_ok=True)
    chemin = os.path.join(CACHE, "%s_%d_%d.tif" % (couverture, x0, y0))
    if os.path.exists(chemin) and os.path.getsize(chemin) > 100:
        with open(chemin, "rb") as f:
            return lire_tiff(f.read())
    erreurs = []
    for version in ([VERSION_OK] if VERSION_OK else ["2.0.1i", "2.0.1", "1.0.0"]):
        try:
            b = telecharger(url_morceau(service, couverture, x0, y0, version))
            if b[:2] not in (b"II", b"MM"):
                raise IOError("réponse non TIFF : " + b[:300].decode("utf-8", "replace"))
            res = lire_tiff(b)
            VERSION_OK = version
            with open(chemin + ".tmp", "wb") as f:
                f.write(b)
            os.replace(chemin + ".tmp", chemin)
            return res
        except Exception as e:
            erreurs.append("WCS %s : %s" % (version, e))
    raise IOError(" | ".join(erreurs))


# ------------------------------------------------------------------ calculs
def points_de_la_carte(dossier=DOSSIER):
    pts = []
    for nom in ("grille.json", "grille_fine.json"):
        chemin = os.path.join(dossier, nom)
        if os.path.exists(chemin):
            with open(chemin, encoding="utf-8") as f:
                pts += [tuple(p) for p in json.load(f)["points"]]
    vus, uniques = set(), []
    for la, lo in pts:
        cle = "%.3f,%.3f" % (la, lo)
        if cle not in vus:
            vus.add(cle)
            uniques.append((la, lo, cle))
    return uniques


def capacite_au_champ(argile, sable, carbone):
    """Eau retenue à la capacité au champ (33 kPa), en % du volume. Saxton & Rawls (2006)."""
    S, C = sable / 100.0, argile / 100.0
    OM = max(0.5, min(8.0, 1.72 * carbone))          # matière organique en %
    t = (-0.251 * S + 0.195 * C + 0.011 * OM + 0.006 * S * OM - 0.027 * C * OM
         + 0.452 * S * C + 0.299)
    theta = t + (1.283 * t * t - 0.374 * t - 0.015)
    return round(100 * max(0.05, min(0.55, theta)), 1)


def relever(source, points):
    """Moyenne des relevés valides autour de chaque point pour une couche SoilGrids."""
    service, couverture, facteur = source
    releves = []          # (indice du point, x, y)
    for i, (la, lo, _) in enumerate(points):
        x, y = homolosine(lo, la)
        for j, (dx, dy) in enumerate(DECALAGES):
            releves.append((i, x + dx, y + dy, j))
    par_morceau = {}
    for r in releves:
        cle = (int(math.floor(r[1] / TUILE) * TUILE), int(math.floor(r[2] / TUILE) * TUILE))
        par_morceau.setdefault(cle, []).append(r)
    cles = sorted(par_morceau)
    brut = [[None] * len(DECALAGES) for _ in points]   # valeur de chaque relevé
    rang = {}
    fait = [0]
    echecs = []

    def traiter(cle):
        x0, y0 = cle
        try:
            W, H, vals, nodata = morceau(service, couverture, x0, y0)
        except Exception as e:
            echecs.append(str(e))
            return
        for i, x, y, j in par_morceau[cle]:
            c = int((x - x0) / TUILE * W)
            l = int((y0 + TUILE - y) / TUILE * H)   # la 1re ligne est en haut (nord)
            if 0 <= c < W and 0 <= l < H:
                v = vals[l * W + c]
                if v is None or (nodata is not None and v == nodata) or v < 0:
                    continue
                brut[i][j] = v / facteur
        fait[0] += 1
        if fait[0] % 10 == 0 or fait[0] == len(cles):
            info("       … %d/%d morceaux" % (fait[0], len(cles)))

    # le premier seul, pour trouver la bonne façon d'interroger le service et voir vite s'il y a un souci
    traiter(cles[0])
    if echecs and fait[0] == 0:
        raise IOError(echecs[0])
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(traiter, cles[1:]))
    if echecs:
        info("       %d morceaux n'ont pas pu être lus (ex. : %s)" % (len(echecs), echecs[0][:300]))
    return brut


def main():
    debut = time.time()
    points = points_de_la_carte()
    if not points:
        info("Aucune grille trouvée (grille.json, grille_fine.json) dans " + DOSSIER)
        return 1
    info("Nature du sol pour %d points (SoilGrids 2.0, ISRIC)." % len(points))
    resultats = {}
    for nom, source in PROPRIETES.items():
        info("  %s (%s)…" % (nom, source[0]))
        try:
            resultats[nom] = relever(source, points)
        except Exception as e:
            info("     Échec pour %s : %s" % (nom, e))
            if nom in ("argile", "sable"):
                info("Impossible de continuer sans l'argile et le sable. Réessaie plus tard (le serveur de SoilGrids est peut-être indisponible).")
                return 1
            resultats[nom] = [[None] * len(DECALAGES) for _ in points]

    # un relevé n'est valable que s'il y a de la terre : le service renvoie 0 sur la mer, les lacs
    # et les zones sans donnée (aucun vrai sol n'a 0 % d'argile ET 0 % de sable)
    def moyenne(nom, i, valides):
        v = [resultats[nom][i][j] for j in valides if resultats[nom][i][j] is not None]
        return round(sum(v) / len(v), 1) if v else None

    sortie, manquants = {}, 0
    for i, (la, lo, cle) in enumerate(points):
        valides = [j for j in range(len(DECALAGES))
                   if (resultats["argile"][i][j] or 0) + (resultats["sable"][i][j] or 0) > 0.5]
        a, s = moyenne("argile", i, valides), moyenne("sable", i, valides)
        c, oc = moyenne("cailloux", i, valides), moyenne("carbone", i, valides)
        if a is None or s is None:
            manquants += 1
            continue
        sortie[cle] = [a, s, c if c is not None else 0.0, capacite_au_champ(a, s, oc if oc is not None else 1.5)]
    with open(SORTIE, "w", encoding="utf-8") as f:
        json.dump({"source": "SoilGrids 2.0 (ISRIC), couche 0-5 cm, licence CC BY 4.0",
                   "colonnes": ["argile %", "sable %", "cailloux % du volume", "capacité au champ % du volume"],
                   "points": sortie}, f, separators=(",", ":"))
    info("Terminé en %d s : %d points enregistrés dans sol.json (%d sans donnée : mer, lacs, villes…)." %
         (time.time() - debut, len(sortie), manquants))
    return 0


if __name__ == "__main__":
    sys.exit(main())
