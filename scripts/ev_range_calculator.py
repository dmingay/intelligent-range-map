#!/usr/bin/env python3
"""
Polestar 4 Intelligent Range Calculator — V3
Uses self-hosted Valhalla routing engine for organic road-network contours.
Physics-based energy model drives range; Valhalla provides polygon shape.
"""

import os, sys, json, math, time, logging, requests
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

LOG_FILE = SCRIPT_DIR / "ev_range.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ev_range_v3")

OUTPUT_DIR = Path("/config/www/ev_range")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
GEOJSON_PATH = OUTPUT_DIR / "range_contour.json"
METADATA_PATH = OUTPUT_DIR / "range_metadata.json"

HA_URL = os.getenv("HA_URL", "http://localhost:8123")
HA_TOKEN = os.getenv("HA_TOKEN", "")
OWM_API_KEY = os.getenv("OWM_API_KEY", "")
VALHALLA_URL = os.getenv("VALHALLA_URL", "http://host.docker.internal:8002")
DEFAULT_LAT = float(os.getenv("DEFAULT_LAT", "51.3656"))
DEFAULT_LON = float(os.getenv("DEFAULT_LON", "-0.4139"))

BATTERY_CAPACITY_KWH = 100.0
VEHICLE_MASS_KG = 2435.0
DRAG_COEFFICIENT = 0.28
FRONTAL_AREA_M2 = 2.62
ROLLING_RESISTANCE = 0.009
AIR_DENSITY_KG_M3 = 1.225
DRIVETRAIN_EFFICIENCY = 0.90
REGEN_EFFICIENCY = 0.65
AUX_POWER_KW = 0.4
RESERVE_SOC_PCT = 5.0

HVAC_POWER_MAP = {
    (-30, -10): 5.0, (-10, 0): 4.0, (0, 5): 3.0, (5, 12): 1.5,
    (12, 22): 0.3, (22, 28): 1.5, (28, 35): 3.0, (35, 50): 4.5,
}

ENERGY_BANDS = [
    {"label": "100%", "fraction": 1.00, "color": "#00e5ff"},
    {"label": "75%",  "fraction": 0.75, "color": "#00b0ff"},
    {"label": "50%",  "fraction": 0.50, "color": "#2979ff"},
    {"label": "25%",  "fraction": 0.25, "color": "#7c4dff"},
]


def ha_get_state(entity_id):
    try:
        r = requests.get(f"{HA_URL}/api/states/{entity_id}",
                         headers={"Authorization": f"Bearer {HA_TOKEN}"}, timeout=10)
        r.raise_for_status()
        state = r.json().get("state")
        return None if state in ("unknown", "unavailable", None) else state
    except Exception as e:
        log.warning(f"Failed to read {entity_id}: {e}")
        return None


def get_vehicle_state():
    soc_raw = ha_get_state("sensor.polestar_3988_battery_charge_level")
    soh_raw = ha_get_state("sensor.final_battery_health_estimate")
    oem_range_raw = ha_get_state("sensor.polestar_3988_estimated_range")
    odo_raw = ha_get_state("sensor.polestar_3988_current_odometer")
    charging_raw = ha_get_state("sensor.polestar_3988_charging_status")
    soc = float(soc_raw) if soc_raw else 80.0
    soh = float(soh_raw) if soh_raw else 95.0
    oem_range = float(oem_range_raw) if oem_range_raw else None
    odometer = float(odo_raw) if odo_raw else None
    charging = charging_raw or "Unknown"
    log.info(f"Vehicle: SoC={soc}%, SoH={soh}%, OEM range={oem_range}, charging={charging}")
    return {"soc": soc, "soh": soh, "oem_range_km": oem_range,
            "odometer_km": odometer, "charging_status": charging}


def get_vehicle_position():
    for entity in ["device_tracker.polestar_3988", "device_tracker.polestar_3988_position"]:
        state = ha_get_state(entity)
        if state and state not in ("home", "not_home"):
            try:
                r = requests.get(f"{HA_URL}/api/states/{entity}",
                                 headers={"Authorization": f"Bearer {HA_TOKEN}"}, timeout=10)
                attrs = r.json().get("attributes", {})
                lat, lon = attrs.get("latitude"), attrs.get("longitude")
                if lat and lon:
                    log.info(f"GPS from {entity}: {lat}, {lon}")
                    return float(lat), float(lon)
            except Exception:
                pass
    log.info(f"No GPS available, using default: {DEFAULT_LAT}, {DEFAULT_LON}")
    return DEFAULT_LAT, DEFAULT_LON


def get_weather(lat, lon):
    default = {"temp_c": 15.0, "wind_ms": 3.0, "description": "Unknown", "icon": "01d"}
    if not OWM_API_KEY:
        log.warning("No OWM API key")
        return default
    try:
        r = requests.get("https://api.openweathermap.org/data/2.5/weather",
                         params={"lat": lat, "lon": lon, "appid": OWM_API_KEY, "units": "metric"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        weather = {
            "temp_c": data["main"]["temp"],
            "wind_ms": data.get("wind", {}).get("speed", 3.0),
            "humidity": data["main"].get("humidity", 50),
            "description": data["weather"][0]["description"] if data.get("weather") else "Unknown",
            "icon": data["weather"][0].get("icon", "01d") if data.get("weather") else "01d",
        }
        log.info(f"Weather: {weather['temp_c']:.1f}C, wind {weather['wind_ms']:.1f}m/s, {weather['description']}")
        return weather
    except Exception as e:
        log.warning(f"Weather fetch failed: {e}")
        return default


def estimate_hvac_power(temp_c):
    for (lo, hi), power in HVAC_POWER_MAP.items():
        if lo <= temp_c < hi:
            return power
    return 2.0


def compute_energy_consumption_kwh_per_km(speed_kmh=60.0, temp_c=15.0, wind_ms=0.0, elev=0.0):
    v_ms = speed_kmh / 3.6
    effective_v = v_ms + (wind_ms * 0.5)
    f_aero = 0.5 * AIR_DENSITY_KG_M3 * DRAG_COEFFICIENT * FRONTAL_AREA_M2 * effective_v ** 2
    f_roll = ROLLING_RESISTANCE * VEHICLE_MASS_KG * 9.81
    f_grade = VEHICLE_MASS_KG * 9.81 * (elev / 1000.0)
    f_total = f_aero + f_roll + max(f_grade, 0)
    regen_recovery = abs(f_grade) * v_ms * REGEN_EFFICIENCY / 1000.0 if f_grade < 0 else 0.0
    p_mech = f_total * v_ms / 1000.0
    p_elec = p_mech / DRIVETRAIN_EFFICIENCY
    p_hvac = estimate_hvac_power(temp_c)
    p_total = max(p_elec + p_hvac + AUX_POWER_KW - regen_recovery, 0.5)
    return p_total / speed_kmh


def calculate_range_km(soc, soh, temp_c=15.0, wind_ms=0.0, energy_fraction=1.0):
    usable_soc = max(soc - RESERVE_SOC_PCT, 0)
    usable_kwh = BATTERY_CAPACITY_KWH * (soh / 100.0) * (usable_soc / 100.0) * energy_fraction
    c1 = compute_energy_consumption_kwh_per_km(35, temp_c, wind_ms, 2.0)
    c2 = compute_energy_consumption_kwh_per_km(60, temp_c, wind_ms, 5.0)
    c3 = compute_energy_consumption_kwh_per_km(100, temp_c, wind_ms, 3.0)
    avg = 0.40 * c1 + 0.40 * c2 + 0.20 * c3
    return max(usable_kwh / avg, 0) if avg > 0 else 0


def fetch_valhalla_isodistance(lat, lon, distances_km):
    """Fetch isodistance polygons from self-hosted Valhalla in a single request."""
    contours = [{"distance": d} for d in distances_km]
    data = {"locations": [{"lat": lat, "lon": lon}], "costing": "auto",
            "contours": contours, "polygons": True}
    dist_str = ", ".join([f"{d:.0f}km" for d in distances_km])
    log.info(f"Valhalla isodistance: {len(contours)} contours, distances=[{dist_str}]")
    try:
        r = requests.post(f"{VALHALLA_URL}/isochrone", json=data, timeout=120)
        r.raise_for_status()
        geojson = r.json()
        features = geojson.get("features", [])
        log.info(f"  Got {len(features)} features from Valhalla")
        geom_by_distance = {}
        for feat in features:
            props = feat.get("properties", {})
            contour_val = props.get("contour")
            geom = feat.get("geometry")
            if contour_val is not None and geom:
                geom_by_distance[float(contour_val)] = geom
                log.info(f"  Contour {contour_val}km: {geom['type']}")
        # Match by closest distance (Valhalla may round slightly differently)
        results = []
        for d in distances_km:
            match = None
            for vd, geom in geom_by_distance.items():
                if abs(vd - d) < 1.0:  # within 1km tolerance
                    match = geom
                    break
            results.append(match)
        return results
    except requests.exceptions.ConnectionError:
        log.error(f"Cannot connect to Valhalla at {VALHALLA_URL}")
        return [None] * len(distances_km)
    except requests.exceptions.HTTPError as e:
        body = e.response.text[:300] if e.response else "N/A"
        log.error(f"Valhalla HTTP error: {e} -- {body}")
        return [None] * len(distances_km)
    except Exception as e:
        log.error(f"Valhalla request failed: {e}")
        return [None] * len(distances_km)


def build_geojson(lat, lon, bands):
    features = []
    for band in bands:
        geom = band.get("geometry")
        if not geom:
            continue
        features.append({"type": "Feature", "properties": {
            "band": band["label"], "range_km": round(band["range_km"], 1),
            "range_miles": round(band["range_km"] * 0.621371, 1),
            "color": band["color"], "fraction": band["fraction"]},
            "geometry": geom})
    features.append({"type": "Feature",
        "properties": {"type": "vehicle", "label": "Polestar 4"},
        "geometry": {"type": "Point", "coordinates": [lon, lat]}})
    return {"type": "FeatureCollection", "features": features}


def main():
    start_time = time.time()
    log.info("=" * 60)
    log.info("Polestar 4 Range Calculator V3 -- Valhalla Edition")
    log.info("=" * 60)

    vehicle = get_vehicle_state()
    soc, soh = vehicle["soc"], vehicle["soh"]
    lat, lon = get_vehicle_position()
    weather = get_weather(lat, lon)
    temp_c, wind_ms = weather["temp_c"], weather["wind_ms"]

    band_results = []
    for band in ENERGY_BANDS:
        range_km = calculate_range_km(soc=soc, soh=soh, temp_c=temp_c,
                                       wind_ms=wind_ms, energy_fraction=band["fraction"])
        band_results.append({**band, "range_km": range_km})
        log.info(f"  Band {band['label']}: {range_km:.1f} km ({range_km * 0.621371:.1f} mi)")

    distances_km, valid_indices = [], []
    for i, band in enumerate(band_results):
        if band["range_km"] >= 1.0:
            distances_km.append(round(band["range_km"], 1))
            valid_indices.append(i)
        else:
            band["geometry"] = None

    if distances_km:
        geometries = fetch_valhalla_isodistance(lat, lon, distances_km)
        for idx, geom in zip(valid_indices, geometries):
            band_results[idx]["geometry"] = geom

    geojson = build_geojson(lat, lon, band_results)
    with open(GEOJSON_PATH, "w") as f:
        json.dump(geojson, f)
    log.info(f"GeoJSON written: {GEOJSON_PATH}")

    max_range = band_results[0]["range_km"] if band_results else 0
    metadata = {
        "timestamp": datetime.now().isoformat(), "version": "3.0",
        "vehicle": {"soc_pct": soc, "soh_pct": soh, "oem_range_km": vehicle["oem_range_km"],
                     "odometer_km": vehicle["odometer_km"], "charging_status": vehicle["charging_status"]},
        "position": {"lat": lat, "lon": lon,
                      "source": "gps" if (lat != DEFAULT_LAT or lon != DEFAULT_LON) else "default"},
        "weather": {"temp_c": weather["temp_c"], "wind_ms": weather["wind_ms"],
                     "description": weather["description"], "icon": weather.get("icon", "01d")},
        "range": {"intelligent_range_km": round(max_range, 1),
                  "intelligent_range_miles": round(max_range * 0.621371, 1),
                  "bands": [{"label": b["label"], "range_km": round(b["range_km"], 1),
                              "range_miles": round(b["range_km"] * 0.621371, 1),
                              "color": b["color"], "has_polygon": b.get("geometry") is not None}
                             for b in band_results]},
        "calculation": {"method": "valhalla_isodistance_v3.0", "energy_model": "physics_mixed_driving",
                         "valhalla_url": VALHALLA_URL,
                         "duration_seconds": round(time.time() - start_time, 1)},
    }
    with open(METADATA_PATH, "w") as f:
        json.dump(metadata, f, indent=2)
    log.info(f"Metadata written: {METADATA_PATH}")

    elapsed = time.time() - start_time
    log.info(f"Complete in {elapsed:.1f}s -- Range: {max_range:.1f} km / {max_range * 0.621371:.1f} mi")
    print(json.dumps({"status": "ok", "range_km": round(max_range, 1),
                       "range_miles": round(max_range * 0.621371, 1), "soc": soc,
                       "bands": len([b for b in band_results if b.get("geometry")]),
                       "duration": round(elapsed, 1)}))


if __name__ == "__main__":
    main()
