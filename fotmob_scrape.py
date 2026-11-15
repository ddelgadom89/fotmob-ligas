"""
Ligas secundarias · FotMob
Por cada liga genera tres CSV (más su histórico):
  data/<liga>/players_latest.csv  -> jugadores de campo (ataque, defensa, top stats), sin porteros
  data/<liga>/gk_latest.csv       -> porteros
  data/<liga>/teams_latest.csv    -> equipos
"""
import os
import re
import json
import time
import datetime as dt

import pandas as pd
from curl_cffi import requests

# ---------------------------------------------------------------------------
# LIGAS: nombre de carpeta -> (id FotMob, slug de la URL)
# Para añadir una liga: copia su id de la URL de FotMob (fotmob.com/leagues/<id>/...).
# Para desactivarla: pon # delante.
LEAGUES = {
    "serie_b":        (86,  "serie-b"),
    "laliga2":        (140, "laliga2"),
    "primeira_liga":  (61,  "liga-portugal"),
    "eliteserien":    (59,  "eliteserien"),
    "allsvenskan":    (67,  "allsvenskan"),
    "superligaen":    (46,  "superligaen"),
    "veikkausliiga":  (51,  "veikkausliiga"),
    # "eredivisie":   (57,  "eredivisie"),
    # "belgium":      (40,  "first-division-a"),
    # "austria":      (38,  "bundesliga"),
    # "switzerland":  (69,  "super-league"),
}
# ---------------------------------------------------------------------------

GK_STATS = ["saves", "_save_percentage", "_goals_prevented", "goals_conceded", "clean_sheet",
            "penalty_save", "total_keeper_sweeper", "total_high_claim"]
GK_EXTRA = ["rating", "mins_played", "accurate_pass", "accurate_long_balls"]
SOLO_PORTEROS = ["saves", "_save_percentage", "_goals_prevented"]
OUT_DIR = "data"

s = requests.Session(impersonate="chrome")


def buscar(obj, clave):
    if isinstance(obj, dict):
        if clave in obj:
            return obj[clave]
        for v in obj.values():
            r = buscar(v, clave)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = buscar(v, clave)
            if r is not None:
                return r


def buscar_urls(obj, out):
    if isinstance(obj, dict):
        if isinstance(obj.get("fetchAllUrl"), str):
            out.add(obj["fetchAllUrl"])
        for v in obj.values():
            buscar_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            buscar_urls(v, out)
    return out


def datos_liga(league_id, slug):
    for url in (f"https://www.fotmob.com/api/data/leagues?id={league_id}",
                f"https://www.fotmob.com/api/leagues?id={league_id}"):
        r = s.get(url, timeout=30)
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                pass
    r = s.get(f"https://www.fotmob.com/leagues/{league_id}/stats/{slug}/players", timeout=30)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError(f"Sin datos (HTTP {r.status_code})")
    return json.loads(m.group(1))


def descargar(stats):
    """Descarga varias categorías y devuelve un DataFrame en formato largo."""
    filas = []
    for stat, url in stats.items():
        try:
            j = s.get(url, timeout=30).json()
        except Exception as e:
            print(f"    fallo en {stat}: {e}")
            continue
        for lista in j.get("TopLists", []):
            for p in lista.get("StatList", []):
                filas.append({
                    "id": p.get("ParticiantId") or p.get("ParticipantId"),
                    "name": p.get("ParticipantName"),
                    "team_id": p.get("TeamId"),
                    "team": p.get("TeamName") or p.get("ParticipantName"),
                    "minutes": p.get("MinutesPlayed"),
                    "matches": p.get("MatchesPlayed"),
                    "stat": stat,
                    "value": p.get("StatValue"),
                    "sub_value": p.get("SubStatValue"),
                })
        time.sleep(1)
    return pd.DataFrame(filas)


def a_tabla(largo, id_col, nombre_col, extra_cols):
    """Pasa de formato largo a una fila por jugador/equipo."""
    largo = largo.rename(columns={"id": id_col, "name": nombre_col})
    orden = "minutes" if largo["minutes"].notna().any() else "matches"
    base = largo.sort_values(orden, ascending=False).drop_duplicates(id_col)[[id_col, nombre_col] + extra_cols]
    valores = largo.pivot_table(index=id_col, columns="stat", values="value", aggfunc="first")
    subs = largo.pivot_table(index=id_col, columns="stat", values="sub_value", aggfunc="first")
    subs.columns = [f"{c}_sub" for c in subs.columns]
    df = base.merge(valores, on=id_col, how="left").merge(subs, on=id_col, how="left")
    return df.dropna(axis=1, how="all")


def guardar(df, liga, nombre, temporada):
    fecha = dt.date.today().isoformat()
    df.insert(0, "fecha", fecha)
    df.insert(1, "season", temporada)
    carpeta = f"{OUT_DIR}/{liga}"
    os.makedirs(carpeta, exist_ok=True)
    df.to_csv(f"{carpeta}/{nombre}_latest.csv", index=False)
    hist_path = f"{carpeta}/{nombre}_history.csv"
    if os.path.exists(hist_path):
        hist = pd.read_csv(hist_path)
        df = pd.concat([hist[hist["fecha"] != fecha], df], ignore_index=True)
    df.to_csv(hist_path, index=False)


def procesar_liga(liga, league_id, slug):
    data = datos_liga(league_id, slug)
    detalles = buscar(data, "details") or {}
    nombre_real = detalles.get("name", "?") if isinstance(detalles, dict) else "?"
    temporada = buscar(data, "selectedSeason") or ""
    print(f"\n=== {liga} -> FotMob: {nombre_real} {temporada} (id {league_id}) ===")

    urls = buscar_urls(data, set())
    stats = {u.rsplit("/", 1)[-1].replace(".json", ""): u for u in urls if "/stats/" in u}
    team_stats = {k: u for k, u in stats.items() if "_team" in k}
    player_stats = {k: u for k, u in stats.items() if "_team" not in k}

    # --- Jugadores (campo + porteros, una sola descarga) ---
    largo = descargar(player_stats)
    if not largo.empty:
        porteros = set(largo.loc[largo["stat"].isin(SOLO_PORTEROS), "id"])
        campo = largo[~largo["id"].isin(porteros) & ~largo["stat"].isin(GK_STATS)]
        gk = largo[largo["id"].isin(porteros) & largo["stat"].isin(GK_STATS + GK_EXTRA)]
        cols = ["team_id", "team", "minutes", "matches"]
        if not campo.empty:
            df = a_tabla(campo, "player_id", "player", cols).sort_values("minutes", ascending=False)
            guardar(df, liga, "players", temporada)
            print(f"  players: {len(df)} jugadores, {df.shape[1]} columnas")
        if not gk.empty:
            df = a_tabla(gk, "player_id", "player", cols).sort_values("minutes", ascending=False)
            guardar(df, liga, "gk", temporada)
            print(f"  gk: {len(df)} porteros")

    # --- Equipos ---
    largo_t = descargar(team_stats)
    if not largo_t.empty:
        largo_t["stat"] = largo_t["stat"].str.replace("_team", "").str.strip("_")
        largo_t["id"] = largo_t["team_id"].fillna(largo_t["id"])
        df = a_tabla(largo_t, "team_id_", "team_name", ["matches"]).rename(
            columns={"team_id_": "team_id", "team_name": "team"}).sort_values("team")
        guardar(df, liga, "teams", temporada)
        print(f"  teams: {len(df)} equipos")


def main():
    errores = []
    for liga, (league_id, slug) in LEAGUES.items():
        try:
            procesar_liga(liga, league_id, slug)
        except Exception as e:
            print(f"ERROR en {liga}: {e}")
            errores.append(liga)
        time.sleep(3)
    print(f"\nTerminado. Ligas con error: {errores or 'ninguna'}")
    if len(errores) == len(LEAGUES):
        raise SystemExit("Fallaron todas las ligas.")


if __name__ == "__main__":
    main()
