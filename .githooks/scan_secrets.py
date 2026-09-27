"""Scanne le depot avec TruffleHog et bloque le commit si un secret est trouve.

Appele par .githooks/pre-commit. Cherche le binaire TruffleHog sur le PATH,
puis a l'emplacement par defaut ~/tools/trufflehog/trufflehog.exe (voir
https://github.com/trufflesecurity/trufflehog/releases pour l'installer).
"""

import json
import pathlib
import shutil
import subprocess
import sys


def find_trufflehog():
    exe = shutil.which("trufflehog") or shutil.which("trufflehog.exe")
    if exe:
        return exe
    candidate = pathlib.Path.home() / "tools" / "trufflehog" / "trufflehog.exe"
    if candidate.exists():
        return str(candidate)
    return None


def main():
    trufflehog = find_trufflehog()
    if not trufflehog:
        print("pre-commit: TruffleHog introuvable (PATH ou ~/tools/trufflehog/trufflehog.exe).")
        print("  -> Installe-le : https://github.com/trufflesecurity/trufflehog/releases")
        print("  -> Ou, si tu es sur qu'il n'y a rien a signaler : git commit --no-verify")
        return 1

    repo_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    ).stdout.strip()

    result = subprocess.run(
        [trufflehog, "filesystem", repo_root, "--no-update", "--json"],
        capture_output=True,
        text=True,
    )

    verified = 0
    unverified = 0
    findings = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("msg") == "finished scanning":
            verified += obj.get("verified_secrets", 0)
            unverified += obj.get("unverified_secrets", 0)
        elif "DetectorName" in obj:
            findings.append(obj)

    if verified or unverified or findings:
        print(f"pre-commit: TruffleHog a trouve {verified} secret(s) verifie(s) et {unverified} non verifie(s).")
        for f in findings:
            print(" -", f.get("DetectorName"), f.get("SourceMetadata"))
        print()
        print("Commit bloque. Retire le secret avant de recommencer,")
        print("ou 'git commit --no-verify' si c'est un faux positif assume.")
        return 1

    print("pre-commit: TruffleHog OK, aucun secret detecte.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
