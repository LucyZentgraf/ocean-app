import streamlit as st
import geopandas as gpd
import pandas as pd
import osmnx as ox
from osmnx.features import features_from_polygon
from shapely.geometry import Polygon, Point, LineString
from shapely.ops import nearest_points
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import folium
from folium import plugins
from streamlit_folium import st_folium
from datetime import datetime
from rapidfuzz import process
import time

# --- Page Setup ---
st.set_page_config(layout="wide")
st.title("OCEAN Demo v.0.05.00")

if "turfcut_counter" not in st.session_state:
    st.session_state.turfcut_counter = 1

def generate_turfcut_id(number=None):
    year_code = datetime.now().strftime("%y")
    if number is None:
        number = st.session_state.turfcut_counter
        st.session_state.turfcut_counter += 1
    return f"TcID{year_code}{number:04d}"

# --- Step 1: Upload Optional CSV ---
uploaded_csv = st.file_uploader("Upload CSV with member data (columns: 'address', 'member name', 'comment')", type="csv")
df = None
if uploaded_csv:
    with st.spinner("Loading CSV..."):
        df = pd.read_csv(uploaded_csv)
        required_cols = {"address", "member name", "comment"}
        if not required_cols.issubset(df.columns):
            st.error("CSV must include: 'address', 'member name', 'comment'")
            df = None
        else:
            st.success(f"Loaded {len(df)} records.")
            st.dataframe(df.head())

# --- Step 2: Draw Turf Area + Route Line + Markers ---
st.markdown("### 🗺️ Draw Turf Area and Route Line (optional)")

with st.expander("Draw polygon (turf), line (route), and marker(s)"):
    m = folium.Map(location=[40.7128, -74.006], zoom_start=13)
    draw = plugins.Draw(
        export=True,
        draw_options={
            "polyline": True,
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
route_line = None
start_point = None
end_point = None

if output and output.get("all_drawings"):
    for obj in output["all_drawings"]:
        geom_type = obj["geometry"]["type"]
        coords = obj["geometry"]["coordinates"]
        if geom_type == "Polygon":
            polygon = Polygon([(lng, lat) for lng, lat in coords[0]])
        elif geom_type == "LineString":
            route_line = LineString([(lng, lat) for lng, lat in coords])
        elif geom_type == "Point" and isinstance(coords, list) and len(coords) == 2:
            if not start_point:
                start_point = (coords[1], coords[0])
            else:
                end_point = (coords[1], coords[0])
    if polygon:
        st.success("Polygon drawn.")
    if route_line:
        st.success("Route line drawn.")
    if start_point:
        st.success(f"Start point selected: ({start_point[0]:.5f}, {start_point[1]:.5f})")
    if end_point:
        st.success(f"End point selected: ({end_point[0]:.5f}, {end_point[1]:.5f})")

# --- Step 3: OSM Building Extraction ---
buildings_gdf = None
if polygon:
    with st.spinner("Pulling building footprints..."):
        try:
            tags = {"building": True}
            buildings_gdf = features_from_polygon(polygon, tags)
            buildings_gdf = buildings_gdf[buildings_gdf.geometry.is_valid]
            buildings_gdf = buildings_gdf[buildings_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]

            def simplify_geom(geom):
                if geom.geom_type == 'MultiPolygon':
                    if len(geom.geoms) == 0:
                        return None
                    return max(geom.geoms, key=lambda a: a.area)
                return geom

            buildings_gdf['geometry'] = buildings_gdf['geometry'].apply(simplify_geom)
            buildings_gdf = buildings_gdf[buildings_gdf['geometry'].notnull()]
            buildings_gdf = buildings_gdf[~buildings_gdf['geometry'].is_empty]
            buildings_gdf = buildings_gdf[~buildings_gdf["building"].isin([
                "commercial", "industrial", "retail", "garage", "service", "warehouse", "school", "university"
            ])]

            if buildings_gdf.empty:
                st.warning("No buildings found. Trying fallback...")
                tags = {"man_made": "building"}
                buildings_gdf = features_from_polygon(polygon, tags)
                buildings_gdf['geometry'] = buildings_gdf['geometry'].apply(simplify_geom)
                buildings_gdf = buildings_gdf[buildings_gdf['geometry'].notnull()]

            if len(buildings_gdf) > 99:
                st.warning("Limiting to 99 buildings for performance.")
                buildings_gdf = buildings_gdf.head(99)

            st.success(f"{len(buildings_gdf)} building footprints loaded.")
        except Exception as e:
            st.error(f"Error pulling OSM data: {e}")

# --- Step 4: Geocode & Match ---
if buildings_gdf is not None and not buildings_gdf.empty:
    with st.spinner("Processing and matching addresses..."):
        geolocator = Nominatim(user_agent="sidewalksort")
        geocode = RateLimiter(geolocator.reverse, min_delay_seconds=1)

        flag_list, address_list, name_list, comment_list, note_list = [], [], [], [], []
        cache = {}
        total = len(buildings_gdf)
        progress = st.progress(0, text="Starting geocoding...")

        for i, (_, row) in enumerate(buildings_gdf.iterrows()):
            geom = row.geometry
            centroid = geom.centroid
            key = f"{centroid.y:.5f},{centroid.x:.5f}"
            addr = row.get("addr:housenumber", "") + " " + row.get("addr:street", "")
            addr = addr.strip()
            resolved, flag, note = "", "", ""
            member_name, comment = "", ""

            try:
                if df is not None and addr:
                    match, score, _ = process.extractOne(addr, df["address"], score_cutoff=85) or (None, None, None)
                    matched_row = df[df["address"] == match] if match else pd.DataFrame()
                    if not matched_row.empty:
                        member_name = matched_row["member name"].values[0]
                        comment = matched_row["comment"].values[0]
                        resolved = addr
                        flag = "match"
                        note = "Matched from OSM"
                    else:
                        flag = "fallback"
                        resolved = addr
                        note = "No match found"
                elif addr:
                    resolved = addr
                    flag = "raw"
                    note = "Unmatched OSM address"
                else:
                    if key in cache:
                        resolved = cache[key]
                    else:
                        loc = geocode((centroid.y, centroid.x))
                        resolved = loc.address if loc else "Unknown"
                        cache[key] = resolved
                    flag = "reverse"
                    note = "Reverse geocoded"

            except Exception as e:
                resolved = "Error"
                flag = "error"
                note = str(e)

            flag_list.append(flag)
            address_list.append(resolved)
            name_list.append(member_name)
            comment_list.append(comment)
            note_list.append(note)

            progress.progress((i + 1) / total, text=f"Geocoding: {(i + 1)}/{total}")

        progress.progress(1.0, text="Geocoding Complete")

        buildings_gdf["address"] = address_list
        buildings_gdf["member_name"] = name_list
        buildings_gdf["comment"] = comment_list
        buildings_gdf["flag"] = flag_list
        buildings_gdf["note"] = note_list

# --- Step 5: Routing ---
        if route_line:
            def project(pt): return route_line.project(route_line.interpolate(route_line.project(pt)))
            buildings_gdf["order"] = buildings_gdf.geometry.centroid.apply(project)
            buildings_gdf = buildings_gdf.sort_values("order")
        elif start_point:
            start_geom = Point(start_point[1], start_point[0])
            buildings_gdf["order"] = buildings_gdf.geometry.centroid.distance(start_geom)
            buildings_gdf = buildings_gdf.sort_values("order")
        else:
            buildings_gdf["order"] = range(len(buildings_gdf))

# --- Step 6: Output Table & CSV ---
        st.markdown("### 📋 Final Sorted Address List")
        output_df = buildings_gdf[["address", "member_name", "comment", "flag", "note"]]
        st.dataframe(output_df, height=400)

        csv = output_df.to_csv(index=False)
        st.download_button("📥 Download CSV", csv, file_name="turfcut_results.csv", mime="text/csv")

# --- Footer ---
st.markdown("---")
st.caption("© 2025 Lucy Zentgraf. All rights reserved.")

