"""Prepare les donnees officielles ARCEP (open data) pour la Vienne (86).

Telecharge le fichier officiel de couverture theorique 4G Orange pour la France
metropolitaine (source : https://data.arcep.fr/mobile/couvertures_theoriques/),
le decompresse, et n'en garde que le departement de la Vienne pour produire
data/vienne_orange_4g_precise.gpkg -- le fichier utilise par server.py.

A relancer a chaque nouvelle publication trimestrielle de l'ARCEP (voir le
calendrier sur https://www.data.gouv.fr/datasets/mon-reseau-mobile) : il
suffit de mettre a jour SOURCE_URL ci-dessous avec le lien du nouveau
trimestre (dossier .../couvertures_theoriques/last/Metropole/00_Metropole/).

Dependances :
    pip install geopandas py7zr shapely pyproj

Usage :
    python prepare_data.py
"""

import tempfile
import urllib.request
from pathlib import Path

import geopandas as gpd
import py7zr

# Lien vers le fichier .gpkg.7z du dernier trimestre publie (Orange, 4G, data,
# France metropolitaine). A mettre a jour a chaque nouveau trimestre.
SOURCE_URL = (
    "https://data.arcep.fr/mobile/couvertures_theoriques/last/Metropole/"
    "00_Metropole/2026_T2_couv_Metropole_OF_4G_data.gpkg.7z"
)

DEPARTEMENT_INSEE = "86"  # Vienne

DATA_DIR = Path(__file__).parent / "data"
OUTPUT_GPKG = DATA_DIR / "vienne_orange_4g_precise.gpkg"


def main():
    DATA_DIR.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        archive_path = tmp / "source.gpkg.7z"

        print(f"Telechargement depuis {SOURCE_URL} ...")
        urllib.request.urlretrieve(SOURCE_URL, archive_path)
        print(f"  -> {archive_path.stat().st_size / 1e6:.1f} Mo")

        print("Decompression...")
        with py7zr.SevenZipFile(archive_path, mode="r") as archive:
            archive.extractall(path=tmp)

        gpkg_files = list(tmp.glob("*.gpkg"))
        if not gpkg_files:
            raise RuntimeError("Aucun fichier .gpkg trouve dans l'archive telechargee.")
        source_gpkg = gpkg_files[0]

        print(f"Filtrage sur le departement {DEPARTEMENT_INSEE}...")
        gdf = gpd.read_file(source_gpkg, where=f"dept='{DEPARTEMENT_INSEE}'")
        print(f"  -> {len(gdf)} polygones (niveaux : {sorted(gdf['niveau'].unique())})")

        gdf.to_file(OUTPUT_GPKG, driver="GPKG")
        print(f"Ecrit : {OUTPUT_GPKG} ({OUTPUT_GPKG.stat().st_size / 1e6:.1f} Mo)")


if __name__ == "__main__":
    main()
