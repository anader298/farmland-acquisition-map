-- Area-weighted soil productivity index per parcel (DuckDB + spatial extension).
--
-- This is the production overlay step. The sample ships its result
-- precomputed in sample/overlays.csv, because the NRCS soil polygons
-- (1,750,773 map units statewide) are far too large for a demo repo.
--
-- Inputs, both in EPSG:5070 (Albers equal area, metres):
--   parcels(pid, g)            parcel polygons
--   soil_mu(mukey, g)          SSURGO map-unit polygons
--   soil_pi(mukey, pi)         productivity index per map unit (Bulletin 811);
--                              NULL for water, urban land and unrated soils
--
-- Two honesty rules:
--   * pi_coverage is 0.0, not NULL and not 1.0, when no rated soil touches
--     the parcel. least(NULL, 1.0) returns 1.0 in DuckDB, which would hand a
--     parcel with no data maximum apparent confidence.
--   * LEFT JOIN, so a parcel that meets no soil polygon keeps a row with
--     known coverage 0.0 and unknown PI, rather than disappearing.

INSTALL spatial;
LOAD spatial;

CREATE OR REPLACE TEMP TABLE _pc AS
SELECT pid, g, ST_Area(g) AS pa FROM parcels WHERE g IS NOT NULL;

-- One row per (parcel, soil polygon) pair. Polygons of the same map unit are
-- disjoint, so summing their intersected areas is correct, not a fan-out.
CREATE OR REPLACE TEMP TABLE _xs AS
SELECT p.pid, s.mukey, ST_Area(ST_Intersection(p.g, s.g)) AS a
FROM _pc p
JOIN soil_mu s ON ST_Intersects(p.g, s.g)
WHERE p.pa > 0;

CREATE OR REPLACE TEMP TABLE _agg AS
SELECT x.pid,
       sum(x.a)                                         AS soil_a,
       sum(CASE WHEN l.pi IS NOT NULL THEN x.a END)     AS pi_a,
       sum(CASE WHEN l.pi IS NOT NULL THEN x.a * l.pi END) AS w_pi
FROM _xs x LEFT JOIN soil_pi l USING (mukey)
WHERE x.a > 0
GROUP BY x.pid;

CREATE OR REPLACE TABLE parcel_soil AS
SELECT p.pid,
       a.w_pi / nullif(a.pi_a, 0)                       AS pi,
       CASE WHEN a.pi_a IS NULL THEN 0.0
            ELSE least(a.pi_a / p.pa, 1.0) END          AS pi_coverage,
       coalesce(a.soil_a, 0) / p.pa                     AS soil_share,
       p.pa / 4046.8564224                              AS gis_acres
FROM _pc p LEFT JOIN _agg a USING (pid);

-- QC: soil_share well above 1.0 means a polygon was loaded twice.
SELECT count(*) FILTER (WHERE soil_share > 1.01) AS fan_out_suspects,
       count(*) FILTER (WHERE pi IS NULL)          AS no_rated_soil,
       count(*)                                    AS parcels
FROM parcel_soil;
