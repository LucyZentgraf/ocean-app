import streamlit as st
import geopandas as gpd
import pandas as pd
import osmnx as ox
from osmnx.features import features_from_polygon
from shapely.geometry import Polygon, Point, LineString
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import folium
from folium import plugins
from streamlit_folium import st_folium
from datetime import datetime
from rapidfuzz import process

# --- Page Setup ---
st.set_page_config(layout="wide")
st.title("OCEAN Demo v0.05.02")

# Turf Cut ID generator
if "turfcut_counter" not in st.session_state:
    st.session_state.turfcut_counter = 1

def generate_turfcut_id():
    year_code = datetime.now().strftime("%y")
    counter = st.session_state.turfcut_counter
    st.session_state.turfcut_counter += 1
    return f"TcID{year_code}{counter:04d}"

turfcut_id = generate_turfcut_id()

# --- Step 1: Optional CSV Upload ---
uploaded_csv = st.file_uploader("Upload optional CSV with columns: 'address', 'member name', 'comment'", type="csv")
df = None
if uploaded_csv:
    df = pd.read_csv(uploaded_csv)
    required_cols = {"address", "member name", "comment"}
    if not required_cols.issubset(df.columns):
        st.error("CSV must include columns: 'address', 'member name', 'comment'")
        df = None
    else:
        st.success(f"Loaded {len(df)} rows from CSV.")
        st.dataframe(df.head())

# --- Step 2: Draw Area and Route ---
st.markdown("### Draw area of interest and optional route")
with st.expander("Draw polygon, route line, and start/end markers"):
    m = folium.Map(location=[40.7128, -74.0060], zoom_start=13)
    draw = plugins.Draw(
        export=True,
        draw_options={
            "polyline": True,
            "polygon": True,
            "marker": True,
            "rectangle": False,
            "circle": False,
            "circlemarker": False
        }
    )
    draw.add_to(m)
    output = st_folium(m, width=700, height=500, returned_objects=["all_drawings"]) or {}

# --- Step 3: Parse Drawings ---
polygon, route_line, start_point, end_point = None, None, None, None
if output.get("all_drawings"):
    for obj in output["all_drawings"]:
        g = obj["geometry"]
        coords = g["coordinates"]
        if g["type"] == "Polygon":
            polygon = Polygon([(lng, lat) for lng, lat in coords[0]])
        elif g["type"] == "LineString":
            route_line = LineString([(lng, lat) for lng, lat in coords])
        elif g["type"] == "Point":
            if not start_point:
                start_point = (coords[1], coords[0])
            else:
                end_point = (coords[1], coords[0])

    if polygon:
        st.success("✅ Polygon drawn.")
    if route_line:
        st.success("✅ Route line drawn.")
    if start_point:
        st.info(f"Start point: {start_point}")
    if end_point:
        st.info(f"End point: {end_point}")

# --- Step 4: Pull OSM Buildings ---
buildings_gdf = None
if polygon:
    with st.spinner("Pulling building footprints..."):
        try:
            tags = {"building": True}
            buildings_gdf = features_from_polygon(polygon, tags)
            buildings_gdf = buildings_gdf[buildings_gdf.geometry.is_valid]
            buildings_gdf = buildings_gdf[buildings_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]

            def simplify(geom):
                return max(geom.geoms, key=lambda g: g.area) if geom.type == 'MultiPolygon' else geom

            buildings_gdf['geometry'] = buildings_gdf['geometry'].apply(simplify)
            buildings_gdf = buildings_gdf.dropna(subset=['geometry'])
            buildings_gdf = buildings_gdf[~buildings_gdf.geometry.is_empty]

            # Filter non-residential
            nonres = {"commercial", "industrial", "retail", "garage", "service", "warehouse", "school", "university"}
            buildings_gdf = buildings_gdf[~buildings_gdf["building"].isin(nonres)]

            if len(buildings_gdf) > 99:
                st.warning("⚠ Too many buildings. Capping to 99.")
                buildings_gdf = buildings_gdf.head(99)

            st.success(f"{len(buildings_gdf)} valid building footprints loaded.")
        except Exception as e:
            st.error(f"Error loading OSM buildings: {e}")
            buildings_gdf = None

# --- Step 5: Match + Reverse Geocode ---
if buildings_gdf is not None:
    st.markdown("### Processing Buildings")
    geolocator = Nominatim(user_agent="OCEAN_app")
    geocode = RateLimiter(geolocator.reverse, min_delay_seconds=1)
    addr_list, name_list, comment_list, flag_list, note_list = [], [], [], [], []

    # Check which columns are available
    hn_col = "addr:housenumber" if "addr:housenumber" in buildings_gdf.columns else None
    st_col = "addr:street" if "addr:street" in buildings_gdf.columns else None

    address_cache = {}
    total = len(buildings_gdf)
    progress = st.progress(0, text="Geocoding 0%")

    for i, (_, row) in enumerate(buildings_gdf.iterrows()):
        geom = row.geometry
        addr = ""
        if hn_col:
            addr += str(row[hn_col]) + " "
        if st_col:
            addr += str(row[st_col])
        addr = addr.strip()

        resolved, flag, note, name, comment = "", "", "", "", ""
        try:
            if addr and df is not None:
                match, score, _ = process.extractOne(addr, df["address"], score_cutoff=85) or (None, None, None)
                if match:
                    row_match = df[df["address"] == match].iloc[0]
                    resolved = match
                    name = row_match["member name"]
                    comment = row_match["comment"]
                    flag = "match"
                    note = "Matched to CSV"
                else:
                    raise ValueError("No fuzzy match found.")
            elif addr:
                resolved = addr
                flag = "partial"
                note = "Address from OSM only"
            else:
                # Reverse geocode fallback
                key = f"{geom.centroid.y:.5f},{geom.centroid.x:.5f}"
                resolved = address_cache.get(key)
                if not resolved:
                    location = geocode((geom.centroid.y, geom.centroid.x))
                    resolved = location.address if location else "Unknown"
                    address_cache[key] = resolved
                flag = "reverse"
                note = "Reverse geocoded"
        except Exception as e:
            resolved = "Error processing"
            note = f"Error: {e}"
            flag = "error"

        addr_list.append(resolved)
        name_list.append(name)
        comment_list.append(comment)
        flag_list.append(flag)
        note_list.append(note)

        percent = (i + 1) / total
        progress.progress(percent, text=f"Geocoding {int(percent * 100)}%")

    progress.progress(1.0, text="Done")

    buildings_gdf["address"] = addr_list
    buildings_gdf["member name"] = name_list
    buildings_gdf["comment"] = comment_list
    buildings_gdf["flag"] = flag_list
    buildings_gdf["note"] = note_list

    # --- Step 6: Sort by Route or Proximity ---
    if route_line:
        def project(pt):
            return route_line.project(pt)
        buildings_gdf["route_order"] = buildings_gdf.geometry.centroid.apply(project)
        buildings_gdf = buildings_gdf.sort_values("route_order")
    elif start_point:
        sp = Point(start_point[1], start_point[0])
        buildings_gdf["dist"] = buildings_gdf.geometry.centroid.distance(sp)
        buildings_gdf = buildings_gdf.sort_values("dist")
    else:
        buildings_gdf["order"] = list(range(len(buildings_gdf)))

    # --- Step 7: Output Table + Download ---
    st.markdown("### 🧾 Final Address Table")
    final_df = buildings_gdf[["address", "member name", "comment", "flag", "note"]]
    st.dataframe(final_df, height=400)

    csv_data = final_df.to_csv(index=False)
    st.download_button(
        label=f"📥 Download CSV ({turfcut_id})",
        data=csv_data,
        file_name=f"{turfcut_id}_results.csv",
        mime="text/csv"
    )

# --- Footer ---
st.markdown("---")
st.caption("© 2025 Lucy Zentgraf for NYPIRG FUND. All rights reserved.")
