"""Prepare les donnees officielles ARCEP (open data) pour la Vienne (86).

Telecharge les fichiers officiels de couverture theorique 4G pour la France
metropolitaine (source : https://data.arcep.fr/mobile/couvertures_theoriques/),
les decompresse, et n'en garde que le departement de la Vienne pour produire
data/vienne_<operateur>_4g_precise.gpkg -- les fichiers utilises par server.py.

A relancer a chaque nouvelle publication trimestrielle de l'ARCEP (voir le
calendrier sur https://www.data.gouv.fr/datasets/mon-reseau-mobile) : il
suffit de mettre a jour QUARTER ci-dessous avec le nouveau trimestre.

Dependances :
    pip install geopandas py7zr shapely pyproj

Usage :
    python prepare_data.py            # tous les operateurs
    python prepare_data.py sfr        # un seul operateur
"""

import sys
import tempfile
import urllib.request
from pathlib import Path

import geopandas as gpd
import py7zr

QUARTER = "2026_T2"  # a mettre a jour a chaque nouveau trimestre publie
BASE_URL = (
    "https://data.arcep.fr/mobile/couvertures_theoriques/last/Metropole/00_Metropole/"
)

# nom court -> code operateur utilise dans les noms de fichiers ARCEP
OPERATOR_FILE_CODES = {
    "orange": "OF",
    "sfr": "SFR0",
}

DEPARTEMENT_INSEE = "86"  # Vienne

DATA_DIR = Path(__file__).parent / "data"


def prepare(name: str):
    file_code = OPERATOR_FILE_CODES[name]
    url = f"{BASE_URL}{QUARTER}_couv_Metropole_{file_code}_4G_data.gpkg.7z"
    output = DATA_DIR / f"vienne_{name}_4g_precise.gpkg"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        archive_path = tmp / "source.gpkg.7z"

        print(f"[{name}] telechargement depuis {url} ...")
        urllib.request.urlretrieve(url, archive_path)
        print(f"[{name}]   -> {archive_path.stat().st_size / 1e6:.1f} Mo")

        print(f"[{name}] decompression...")
        with py7zr.SevenZipFile(archive_path, mode="r") as archive:
            archive.extractall(path=tmp)

        gpkg_files = list(tmp.glob("*.gpkg"))
        if not gpkg_files:
            raise RuntimeError("Aucun fichier .gpkg trouve dans l'archive telechargee.")

        print(f"[{name}] filtrage sur le departement {DEPARTEMENT_INSEE}...")
        gdf = gpd.read_file(gpkg_files[0], where=f"dept='{DEPARTEMENT_INSEE}'")
        print(f"[{name}]   -> {len(gdf)} polygones (niveaux : {sorted(gdf['niveau'].unique())})")

        gdf.to_file(output, driver="GPKG")
        print(f"[{name}] ecrit : {output} ({output.stat().st_size / 1e6:.1f} Mo)")


def main():
    DATA_DIR.mkdir(exist_ok=True)
    requested = sys.argv[1:] or list(OPERATOR_FILE_CODES)
    unknown = [n for n in requested if n not in OPERATOR_FILE_CODES]
    if unknown:
        raise SystemExit(f"Operateur(s) inconnu(s) : {unknown} (choix : {list(OPERATOR_FILE_CODES)})")
    for name in requested:
        prepare(name)


if __name__ == "__main__":
    main()
