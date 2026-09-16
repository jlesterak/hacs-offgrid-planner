"""Sun position, irradiance decomposition and plane-of-array irradiance. Stdlib only."""
from __future__ import annotations

import datetime as dt
import math

SOLAR_CONSTANT = 1367.0


def position(t_utc: dt.datetime, lat: float, lon: float) -> tuple[float, float]:
    """Solar (elevation, azimuth) in degrees. Azimuth: 0 = N, 90 = E, 180 = S.

    NOAA spreadsheet algorithm (~0.01°), no refraction correction.
    """
    jd = t_utc.timestamp() / 86400 + 2440587.5
    jc = (jd - 2451545) / 36525
    l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    mr = math.radians(m)
    c = (math.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
         + math.sin(2 * mr) * (0.019993 - 0.000101 * jc) + math.sin(3 * mr) * 0.000289)
    omega = 125.04 - 1934.136 * jc
    app_long = l0 + c - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    obliq = (23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
             + 0.00256 * math.cos(math.radians(omega)))
    decl = math.asin(math.sin(math.radians(obliq)) * math.sin(math.radians(app_long)))
    y = math.tan(math.radians(obliq / 2)) ** 2
    l0r = math.radians(l0)
    eot = 4 * math.degrees(y * math.sin(2 * l0r) - 2 * e * math.sin(mr)
                           + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
                           - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr))
    t = t_utc.astimezone(dt.UTC)
    minutes = t.hour * 60 + t.minute + t.second / 60
    ha = math.radians(((minutes + eot + 4 * lon) % 1440) / 4 - 180)
    latr = math.radians(lat)
    cos_zen = math.sin(latr) * math.sin(decl) + math.cos(latr) * math.cos(decl) * math.cos(ha)
    zen = math.acos(max(-1.0, min(1.0, cos_zen)))
    az_den = math.cos(latr) * math.sin(zen)
    if abs(az_den) < 1e-9:
        az = 180.0
    else:
        cos_az = (math.sin(latr) * math.cos(zen) - math.sin(decl)) / az_den
        az = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
        az = (az + 180) % 360 if ha > 0 else (540 - az) % 360
    return 90 - math.degrees(zen), az


def erbs_split(ghi: float, elev_deg: float, t_utc: dt.datetime) -> tuple[float, float]:
    """Split GHI into (beam on the horizontal, diffuse) with the Erbs correlation."""
    if ghi <= 0 or elev_deg <= 0:
        return 0.0, max(ghi, 0.0)
    doy = t_utc.timetuple().tm_yday
    cos_z = max(math.sin(math.radians(elev_deg)), 0.02)
    extra = SOLAR_CONSTANT * (1 + 0.033 * math.cos(2 * math.pi * doy / 365)) * cos_z
    kt = min(ghi / extra, 1.0)
    if kt <= 0.22:
        fd = 1 - 0.09 * kt
    elif kt <= 0.80:
        fd = 0.9511 - 0.1604 * kt + 4.388 * kt**2 - 16.638 * kt**3 + 12.336 * kt**4
    else:
        fd = 0.165
    diffuse = ghi * fd
    return ghi - diffuse, diffuse


def poa(beam_h: float, diffuse: float, elev_deg: float, sun_az: float, tilt_deg: float = 0.0,
        panel_az: float = 180.0, albedo: float = 0.2, b0: float = 0.05) -> float:
    """Plane-of-array irradiance (W/m²): isotropic sky, ground reflection, ASHRAE beam IAM.

    tilt_deg is the panel pitch and panel_az the compass direction its low edge faces.
    """
    if elev_deg <= 0:
        return 0.0
    zen, beta = math.radians(90 - elev_deg), math.radians(tilt_deg)
    cos_aoi = (math.cos(zen) * math.cos(beta)
               + math.sin(zen) * math.sin(beta) * math.cos(math.radians(sun_az - panel_az)))
    dni = beam_h / max(math.cos(zen), 0.087)
    beam = 0.0 if cos_aoi <= 0.087 else dni * cos_aoi * max(0.0, 1 - b0 * (1 / cos_aoi - 1))
    return (beam + diffuse * (1 + math.cos(beta)) / 2
            + (beam_h + diffuse) * albedo * (1 - math.cos(beta)) / 2)
