import streamlit as st
import geopandas as gpd
import pandas as pd
import osmnx as ox
from osmnx.features import features_from_polygon
from shapely.geometry import Polygon
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import folium
from folium import plugins
from streamlit_folium import st_folium
import networkx as nx
from networkx.algorithms.approximation import traveling_salesman_problem
from datetime import datetime
from rapidfuzz import process

# --- Page Setup ---
st.set_page_config(layout="wide")
st.title("OCEAN Demo v.0.04.00")

if "turfcut_counter" not in st.session_state:
    st.session_state.turfcut_counter = 1

def generate_turfcut_id():
    year_code = datetime.now().strftime("%y")
    number = f"{st.session_state.turfcut_counter:04d}"
    return f"TcID{year_code}{number}"

uploaded_csv = st.file_uploader("Upload CSV with member data (columns: 'address', 'member name', 'comment')", type="csv")
df = None
if uploaded_csv:
    df = pd.read_csv(uploaded_csv)
    required_cols = {"address", "member name", "comment"}
    if not required_cols.issubset(df.columns):
        st.error("CSV must include columns: 'address', 'member name', 'comment'")
        df = None
    else:
        st.success(f"Loaded {len(df)} records.")
        st.dataframe(df.head())

st.markdown("Draw Turf Area and Set Start Point")

with st.expander("Click to draw polygon and place start marker"):
    m = folium.Map(location=[40.7128, -74.006], zoom_start=13)
    draw = plugins.Draw(
        export=True,
        draw_options={
            "polyline": False,
            "circle": False,
            "rectangle": False,
            "circlemarker": False,
            "marker": True,
            "polygon": True
        }
    )
    draw.add_to(m)
    output = st_folium(m, width=700, height=500, returned_objects=["all_drawings"])

polygon = None
start_point = None

if output and output.get("all_drawings"):
    for obj in output["all_drawings"]:
        geom_type = obj["geometry"]["type"]
        coords = obj["geometry"]["coordinates"]
        if geom_type == "Polygon":
            polygon = Polygon([(lng, lat) for lng, lat in coords[0]])
        elif geom_type == "Point" and isinstance(coords, list) and len(coords) == 2:
            lng, lat = coords
            start_point = (lat, lng)

    if polygon:
        st.success("Polygon drawn.")
    if start_point:
        st.success(f"Start point selected at ({start_point[0]:.5f}, {start_point[1]:.5f})")

loop_back = st.checkbox("Return to start point in route?", value=True)

buildings_gdf = None
if polygon:
    st.markdown("Pulling building footprints...")
    try:
        tags = {"building": True}
        buildings_gdf = features_from_polygon(polygon, tags)
        buildings_gdf = buildings_gdf[buildings_gdf.geometry.is_valid]
        buildings_gdf = buildings_gdf[buildings_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]

        def simplify_geom(geom):
            if geom.geom_type == 'MultiPolygon':
                return max(geom.geoms, key=lambda a: a.area)
            return geom

        buildings_gdf['geometry'] = buildings_gdf['geometry'].apply(simplify_geom)

        buildings_gdf = buildings_gdf[~buildings_gdf["building"].isin([
            "commercial", "industrial", "retail", "garage", "service", "warehouse", "school", "university"
        ])]
        if buildings_gdf.empty:
            st.warning("No buildings found. Trying structure fallback...")
            tags = {"man_made": "building"}
            buildings_gdf = features_from_polygon(polygon, tags)
            buildings_gdf = buildings_gdf[buildings_gdf.geometry.is_valid]
            buildings_gdf = buildings_gdf[buildings_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]
            buildings_gdf['geometry'] = buildings_gdf['geometry'].apply(simplify_geom)
        st.success(f"{len(buildings_gdf)} building/structure footprints loaded.")

        if len(buildings_gdf) > 99:
            st.warning("Capping building count to 99 for performance.")
            buildings_gdf = buildings_gdf.head(99)

    except Exception as e:
        st.error(f"Error pulling OSM data: {e}")
        buildings_gdf = None

if buildings_gdf is not None and not buildings_gdf.empty:
    flag_list = []
    address_list = []
    member_name_list = []
    comment_list = []
    result_list = []

    geolocator = Nominatim(user_agent="sidewalksort")
    geocode = RateLimiter(geolocator.reverse, min_delay_seconds=1)

    housenumber_col = "addr:housenumber" if "addr:housenumber" in buildings_gdf.columns else None
    street_col = "addr:street" if "addr:street" in buildings_gdf.columns else None

    if housenumber_col and street_col:
        buildings_gdf["osm_address"] = buildings_gdf[housenumber_col].fillna("") + " " + buildings_gdf[street_col].fillna("")
    elif street_col:
        buildings_gdf["osm_address"] = buildings_gdf[street_col].fillna("")
    elif housenumber_col:
        buildings_gdf["osm_address"] = buildings_gdf[housenumber_col].fillna("")
    else:
        buildings_gdf["osm_address"] = ""
    buildings_gdf["osm_address"] = buildings_gdf["osm_address"].str.strip()

    st.markdown("Resolving addresses...")
    address_cache = {}
    progress_bar = st.progress(0, text="Geocoding: 0%")
    total = len(buildings_gdf)

    for i, (_, row) in enumerate(buildings_gdf.iterrows()):
        geom = row.geometry
        addr = row["osm_address"]
        flag, resolved, note, member_name, comment = "", "", "", "", ""

        if df is not None:
            matched_address, score, idx = process.extractOne(addr, df["address"], score_cutoff=85)
            if matched_address:
                matched_row = df[df["address"] == matched_address].head(1)
            else:
                matched_row = pd.DataFrame()
            if not matched_row.empty:
                member_name = matched_row["member name"].values[0]
                comment = matched_row["comment"].values[0]

        if addr and df is not None and matched_row is not None and not matched_row.empty:
            resolved = addr
            flag = "match"
            note = "Matched from OSM"
        elif addr:
            key = f"{geom.centroid.y:.5f},{geom.centroid.x:.5f}"
            if key in address_cache:
                resolved = address_cache[key]
            else:
                try:
                    if geom.is_empty:
                        resolved = "Invalid geometry"
                    else:
                        loc = geocode((geom.centroid.y, geom.centroid.x))
                        resolved = loc.address if loc else "Unknown"
                except Exception as e:
                    resolved = f"Geocode error: {e}"
                address_cache[key] = resolved
            flag = "reverse"
            note = "Reverse geocoded"
        else:
            resolved = "No address"
            flag = "error"
            note = "No data or error"

        flag_list.append(flag)
        address_list.append(resolved)
        member_name_list.append(member_name)
        comment_list.append(comment)
        result_list.append(note)

        percent_complete = int(((i + 1) / total) * 100)
        progress_bar.progress((i + 1) / total, text=f"Geocoding: {percent_complete}%")

    progress_bar.progress(1.0, text="Geocoding: 100%")

    buildings_gdf["address"] = address_list
    buildings_gdf["member_name"] = member_name_list
    buildings_gdf["comment"] = comment_list
    buildings_gdf["flag"] = flag_list
    buildings_gdf["note"] = result_list

    st.markdown("Generating pedestrian route network...")
    # Add routing logic here

st.markdown("---")
st.markdown("Demo made for NYPIRG FUND by Lucy Zentgraf, 2025. All Rights Reserved")
st.session_state.turfcut_counter += 1
