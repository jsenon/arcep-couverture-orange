"""Petit serveur local : couverture mobile theorique ARCEP par coordonnees GPS.

Lance un serveur HTTP local qui sert une page web (static/index.html) et
une API (/api/couverture) qui interroge, cote serveur (donc sans probleme
de CORS), les APIs publiques :
  - geo.api.gouv.fr        -> commune (code INSEE) contenant un point GPS
  - api-adresse.data.gouv.fr -> adresse connue la plus proche d'un point GPS
  - monreseaumobile.arcep.fr -> donnees officielles de couverture theorique
    (API interne du site, utilisee en repli / pour les operateurs autres
    qu'Orange et en dehors de la Vienne)

Pour l'operateur Orange dans le departement de la Vienne (86), les donnees
proviennent directement du jeu de donnees officiel ouvert de l'ARCEP
(polygones de couverture theorique, https://data.arcep.fr/mobile/couvertures_theoriques/),
charge localement depuis data/vienne_orange_4g_precise.gpkg -- voir
prepare_data.py pour regenerer ce fichier depuis une nouvelle publication
trimestrielle.

Dependances supplementaires pour cette partie :
    pip install geopandas shapely pyproj

Usage :
    python server.py
Puis ouvrir http://localhost:8000 dans un navigateur.
"""

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", 8000))
HOST = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
STATIC_DIR = Path(__file__).parent / "static"
DATA_DIR = Path(__file__).parent / "data"
USER_AGENT = "arcep-couverture-locale/1.0 (usage personnel)"

OPERATORS = {
    "20801": "Orange",
    "20810": "SFR",
    "20815": "Free Mobile",
    "20820": "Bouygues Telecom",
}
ALL_OPERATOR_CODES = ",".join(OPERATORS.keys())

NIVEAU_LABELS = {
    "TBC": "Tres bonne couverture",
    "BC": "Bonne couverture",
    "CL": "Couverture limitee",
    "non_couverte": "Zone non couverte",
}
NIVEAU_NOMINAL_PCT = {"TBC": 100, "BC": 70, "CL": 25, "non_couverte": 0}


class LocalVienneData:
    """Donnees officielles ARCEP (polygones) pour Orange 4G, departement 86."""

    def __init__(self):
        self.available = False
        self.gdf = None
        self.bounds_wgs84 = None
        self.to_lambert93 = None
        self.to_wgs84 = None

        precise_path = DATA_DIR / "vienne_orange_4g_precise.gpkg"
        if not precise_path.exists():
            return
        try:
            import geopandas as gpd
            from pyproj import Transformer

            self.gdf = gpd.read_file(precise_path)
            wgs84 = self.gdf.to_crs("EPSG:4326")
            self.bounds_wgs84 = tuple(wgs84.total_bounds)  # west, south, east, north
            self.to_lambert93 = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
            self.to_wgs84 = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
            self.available = True
        except Exception as exc:
            print(f"[donnees officielles Vienne indisponibles] {exc}")

    def covers(self, lat, lon):
        if not self.available:
            return False
        west, south, east, north = self.bounds_wgs84
        return west <= lon <= east and south <= lat <= north

    def point_niveau(self, lat, lon):
        """Renvoie 'TBC' / 'BC' / 'CL' / 'non_couverte', ou None si hors zone."""
        if not self.covers(lat, lon):
            return None
        from shapely.geometry import Point

        x, y = self.to_lambert93.transform(lon, lat)
        pt = Point(x, y)
        for _, row in self.gdf.iterrows():
            if row.geometry.contains(pt):
                return row["niveau"]
        return "non_couverte"

    def bbox_geojson(self, south, west, north, east):
        """Polygones (en WGS84) de couverture, decoupes sur l'emprise demandee."""
        if not self.available:
            return None
        vw, vs, ve, vn = self.bounds_wgs84
        if east < vw or west > ve or north < vs or south > vn:
            return None
        from shapely.geometry import box
        from shapely.ops import transform as shp_transform

        x0, y0 = self.to_lambert93.transform(west, south)
        x1, y1 = self.to_lambert93.transform(east, north)
        clip_box = box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

        features = []
        for _, row in self.gdf.iterrows():
            clipped = row.geometry.intersection(clip_box)
            if clipped.is_empty:
                continue
            clipped_wgs84 = shp_transform(lambda x, y: self.to_wgs84.transform(x, y), clipped)
            niveau = row["niveau"]
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "niveau": niveau,
                        "label": NIVEAU_LABELS.get(niveau, niveau),
                        "color": PALETTE_BY_NIVEAU.get(niveau, PALETTE["inconnue"]),
                    },
                    "geometry": clipped_wgs84.__geo_interface__,
                }
            )
        return {"type": "FeatureCollection", "features": features}


VIENNE_DATA = LocalVienneData()


def http_get_json(url: str, timeout: float = 8.0):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def commune_from_point(lat: float, lon: float):
    """Code INSEE de la commune contenant ce point (point dans polygone)."""
    url = "https://geo.api.gouv.fr/communes?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "fields": "nom,code"}
    )
    data = http_get_json(url)
    if not data:
        return None
    return {"citycode": data[0]["code"], "nom": data[0]["nom"]}


def nearest_known_address(lat: float, lon: float):
    """Adresse connue (base adresse nationale) la plus proche du point."""
    url = "https://api-adresse.data.gouv.fr/reverse/?" + urllib.parse.urlencode(
        {"lon": lon, "lat": lat}
    )
    data = http_get_json(url)
    feats = data.get("features") or []
    if not feats:
        return None
    f = feats[0]
    coords = f["geometry"]["coordinates"]
    return {
        "citycode": f["properties"].get("citycode"),
        "label": f["properties"].get("label") or f["properties"].get("name"),
        "lon": coords[0],
        "lat": coords[1],
    }


def stat_couverture(insee: str, lon: float, lat: float, entite: str):
    params = {
        "id": insee,
        "operators": ALL_OPERATOR_CODES,
        "service": "internet",
        "entite": entite,
    }
    if entite == "adresse":
        params["x"] = lon
        params["y"] = lat
    url = "https://monreseaumobile.arcep.fr/api/stat_couverture/?" + urllib.parse.urlencode(params)
    return http_get_json(url)


def classify(pct):
    # Seuils exacts repris du code source du site officiel ARCEP
    # (page-*.js : t>90 TBC, 50<=t<=90 Bonne, 0<t<50 Limitee, t==0 Non couverte).
    if pct is None:
        return "Inconnue"
    if pct > 90:
        return "Tres bonne couverture"
    if pct >= 50:
        return "Bonne couverture"
    if pct > 0:
        return "Couverture limitee"
    return "Zone non couverte"


def build_operators_list(stat_payload):
    ops = stat_payload["stat"]["operator"]
    vals = stat_payload["stat"]["values_4g"]
    out = []
    for code, val in zip(ops, vals):
        out.append(
            {
                "code": code,
                "name": OPERATORS.get(code, code),
                "pct": val,
                "label": classify(val),
            }
        )
    return out


def lookup_coverage(lat: float, lon: float):
    result = _lookup_coverage_live(lat, lon)

    niveau = VIENNE_DATA.point_niveau(lat, lon)
    if niveau is not None:
        official_orange = {
            "code": "20801",
            "name": "Orange",
            "pct": None,
            "label": NIVEAU_LABELS.get(niveau, niveau),
            "niveau": niveau,
            "source": "officiel_arcep_vienne",
        }
        if result.get("success"):
            result["orange"] = official_orange
            for op in result.get("operators", []):
                if op["code"] == "20801":
                    op.update(official_orange)
        else:
            # aucune donnee live (ex: hors reseau), mais la donnee officielle existe
            result = {
                "success": True,
                "precision": "officiel_vienne",
                "address_label": None,
                "distance_m": None,
                "commune": result.get("commune") if isinstance(result, dict) else None,
                "citycode": None,
                "orange": official_orange,
                "operators": [official_orange],
            }
    return result


def _lookup_coverage_live(lat: float, lon: float):
    commune = None
    try:
        commune = commune_from_point(lat, lon)
    except Exception:
        pass

    # 1) meilleure precision : le point GPS exact tel que fourni, interroge
    #    directement contre la grille ARCEP (pas besoin d'une adresse connue
    #    a cet endroit precis).
    if commune:
        try:
            r = stat_couverture(commune["citycode"], lon, lat, "adresse")
        except Exception:
            r = None
        if r and r.get("success"):
            operators = build_operators_list(r)
            orange = next((o for o in operators if o["code"] == "20801"), None)
            return {
                "success": True,
                "precision": "point",
                "address_label": None,
                "distance_m": 0.0,
                "commune": commune["nom"],
                "citycode": commune["citycode"],
                "orange": orange,
                "operators": operators,
            }

    # 2) repli : adresse connue la plus proche du point (si le point exact
    #    ne renvoie rien, par ex. en dehors de toute zone modelisee)
    try:
        addr = nearest_known_address(lat, lon)
    except Exception:
        addr = None

    if addr and addr.get("citycode"):
        try:
            r = stat_couverture(addr["citycode"], addr["lon"], addr["lat"], "adresse")
        except Exception:
            r = None
        if r and r.get("success"):
            distance = haversine_m(lat, lon, addr["lat"], addr["lon"])
            operators = build_operators_list(r)
            orange = next((o for o in operators if o["code"] == "20801"), None)
            return {
                "success": True,
                "precision": "adresse_proche",
                "address_label": addr["label"],
                "distance_m": round(distance, 1),
                "commune": commune["nom"] if commune else None,
                "citycode": addr["citycode"],
                "orange": orange,
                "operators": operators,
            }

    # 3) dernier repli : moyenne de la commune entiere
    if commune:
        try:
            r = stat_couverture(commune["citycode"], lon, lat, "commune")
        except Exception:
            r = None
        if r and r.get("success"):
            operators = build_operators_list(r)
            orange = next((o for o in operators if o["code"] == "20801"), None)
            return {
                "success": True,
                "precision": "commune",
                "address_label": None,
                "distance_m": None,
                "commune": commune["nom"],
                "citycode": commune["citycode"],
                "orange": orange,
                "operators": operators,
            }

    return {"success": False, "message": "Aucune donnee de couverture ARCEP trouvee pour ce point."}


# Palette officielle ARCEP (recuperee sur la legende "Niveau de couverture"
# de monreseaumobile.arcep.fr, degrade orange utilise pour l'operateur Orange).
PALETTE = {
    "tres_bonne": "#864701",
    "bonne": "#c06600",
    "limitee": "#ff8700",
    "non_couverte": "#f1ede6",
    "inconnue": "#b0b0b0",
}

PALETTE_BY_NIVEAU = {
    "TBC": PALETTE["tres_bonne"],
    "BC": PALETTE["bonne"],
    "CL": PALETTE["limitee"],
    "non_couverte": PALETTE["non_couverte"],
}


def color_for(pct):
    if pct is None:
        return PALETTE["inconnue"]
    if pct > 90:
        return PALETTE["tres_bonne"]
    if pct >= 50:
        return PALETTE["bonne"]
    if pct > 0:
        return PALETTE["limitee"]
    return PALETTE["non_couverte"]


def fetch_grid_point(citycode, lat, lon, operator_code):
    try:
        r = stat_couverture(citycode, lon, lat, "adresse")
    except Exception:
        r = None
    pct = None
    if r and r.get("success"):
        ops = r["stat"]["operator"]
        vals = r["stat"]["values_4g"]
        for code, val in zip(ops, vals):
            if code == operator_code:
                pct = val
                break
    return {
        "lat": lat,
        "lon": lon,
        "pct": pct,
        "color": color_for(pct),
        "label": classify(pct) if pct is not None else "Non modelisee",
    }


NATIVE_CELL_M = 50.0  # maille reelle de la grille ARCEP (mesuree empiriquement,
# par balayage fin des coordonnees : les transitions de valeur tombent tous
# les 50 m, jamais en dessous -- voir la conversation pour le detail de la mesure).
MAX_CELLS_PER_SIDE = 16  # plafond pour rester rapide (<= 256 appels ARCEP)


def coverage_bbox(south, west, north, east, operator_code="20801"):
    """Couverture pour l'emprise de carte demandee.

    Pour Orange dans la Vienne : polygones officiels ARCEP exacts (vecteur),
    decoupes sur l'emprise -- pas d'echantillonnage, precision native.
    Sinon : grille de points echantillonnes via l'API du site, alignee sur la
    vraie maille ARCEP de 50 m (voir plus bas).
    """
    if operator_code == "20801":
        geojson = VIENNE_DATA.bbox_geojson(south, west, north, east)
        if geojson is not None:
            return {
                "success": True,
                "mode": "vector",
                "source": "officiel_arcep_vienne",
                "operator": OPERATORS[operator_code],
                "geojson": geojson,
                "palette": PALETTE,
            }

    return _coverage_bbox_sampled(south, west, north, east, operator_code)


def _coverage_bbox_sampled(south, west, north, east, operator_code="20801"):
    """Grille de couverture couvrant une emprise de carte (bounding box),
    alignee sur la vraie maille ARCEP de 50 m (pas une subdivision arbitraire
    de l'ecran) : chaque cellule a une taille multiple de 50 m, et sa position
    est calee sur une grille absolue (independante du centre demande) pour
    que les tuiles restent stables quand on deplace la carte.

    Le parametre "id" de l'API ARCEP n'a aucune influence sur le resultat en
    mode entite=adresse (verifie empiriquement : seuls x/y comptent), donc un
    seul code INSEE de reference suffit pour toute la grille, meme si
    l'emprise chevauche plusieurs communes.
    """
    center_lat = (south + north) / 2
    center_lon = (west + east) / 2
    commune = commune_from_point(center_lat, center_lon)
    ref_citycode = commune["citycode"] if commune else "75056"

    meters_per_deg_lat = 111320.0
    meters_per_deg_lon = 111320.0 * math.cos(math.radians(center_lat)) or 1e-6

    width_m = (east - west) * meters_per_deg_lon
    height_m = (north - south) * meters_per_deg_lat
    span_m = max(width_m, height_m)

    # plus petit multiple de 50 m qui tient sous le plafond de cellules
    stride_m = NATIVE_CELL_M
    while span_m / stride_m > MAX_CELLS_PER_SIDE:
        stride_m += NATIVE_CELL_M

    lat_cell_deg = stride_m / meters_per_deg_lat
    lon_cell_deg = stride_m / meters_per_deg_lon

    # grille absolue (calee sur lat/lon 0), stable quel que soit le point demande
    i0 = math.floor(south / lat_cell_deg)
    i1 = math.ceil(north / lat_cell_deg)
    j0 = math.floor(west / lon_cell_deg)
    j1 = math.ceil(east / lon_cell_deg)

    grid_coords = []
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            plat = (i + 0.5) * lat_cell_deg
            plon = (j + 0.5) * lon_cell_deg
            grid_coords.append((plat, plon))

    with ThreadPoolExecutor(max_workers=16) as pool:
        points = list(
            pool.map(lambda c: fetch_grid_point(ref_citycode, c[0], c[1], operator_code), grid_coords)
        )

    return {
        "success": True,
        "mode": "grid",
        "source": "estimation_api_site",
        "operator": OPERATORS.get(operator_code, operator_code),
        "cell_lat_deg": lat_cell_deg,
        "cell_lon_deg": lon_cell_deg,
        "cell_size_m": stride_m,
        "native_cell_m": NATIVE_CELL_M,
        "points": points,
        "palette": PALETTE,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str):
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404, "Not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/couverture":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                lat = float(qs["lat"][0])
                lon = float(qs["lon"][0])
            except (KeyError, ValueError, IndexError):
                self._send_json({"success": False, "message": "Parametres lat/lon invalides."}, 400)
                return
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                self._send_json({"success": False, "message": "Coordonnees hors limites."}, 400)
                return
            try:
                result = lookup_coverage(lat, lon)
            except urllib.error.URLError as exc:
                self._send_json({"success": False, "message": f"Erreur reseau : {exc}"}, 502)
                return
            self._send_json(result)
            return

        if parsed.path == "/api/couverture_bbox":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                south = float(qs["south"][0])
                west = float(qs["west"][0])
                north = float(qs["north"][0])
                east = float(qs["east"][0])
            except (KeyError, ValueError, IndexError):
                self._send_json({"success": False, "message": "Parametres de bbox invalides."}, 400)
                return
            if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
                self._send_json({"success": False, "message": "Bbox invalide."}, 400)
                return
            operator_code = qs.get("operator", ["20801"])[0]
            if operator_code not in OPERATORS:
                operator_code = "20801"
            try:
                result = coverage_bbox(south, west, north, east, operator_code)
            except urllib.error.URLError as exc:
                self._send_json({"success": False, "message": f"Erreur reseau : {exc}"}, 502)
                return
            self._send_json(result)
            return

        if parsed.path == "/" or parsed.path == "":
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return

        safe_name = Path(parsed.path).name
        candidate = STATIC_DIR / safe_name
        if candidate.exists() and candidate.is_file():
            content_type = "text/html; charset=utf-8"
            if safe_name.endswith(".js"):
                content_type = "application/javascript; charset=utf-8"
            elif safe_name.endswith(".css"):
                content_type = "text/css; charset=utf-8"
            self._send_file(candidate, content_type)
            return

        self.send_error(404, "Not found")


def main():
    ThreadingHTTPServer.allow_reuse_address = False
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Serveur demarre sur {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
