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
st.title("OCEAN Demo v.0.05")

# TurfCut ID Generator
if "turfcut_counter" not in st.session_state:
    st.session_state.turfcut_counter = 1

def generate_turfcut_id(number=None):
    year_code = datetime.now().strftime("%y")
    if number is None:
        number = st.session_state.turfcut_counter
        st.session_state.turfcut_counter += 1
    return f"TcID{year_code}{number:04d}"

# --- Upload CSV with member data (optional) ---
uploaded_csv = st.file_uploader("Upload CSV with member data (columns: 'address', 'member name', 'comment')", type="csv")
df = None
if uploaded_csv:
    with st.spinner("Loading CSV..."):
        df = pd.read_csv(uploaded_csv)
        required_cols = {"address", "member name", "comment"}
        if not required_cols.issubset(df.columns):
            st.error("CSV must include columns: 'address', 'member name', 'comment'")
            df = None
        else:
            st.success(f"Loaded {len(df)} records.")
            st.dataframe(df.head())

# --- Draw Map: Polygon + Route + Start/End points ---
st.markdown("Draw Turf Area and Set Route")

with st.expander("Click to draw polygon, start/end marker, and route line"):
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
                start_point = (coords[1], coords[0])  # lat, lng order
            else:
                end_point = (coords[1], coords[0])

    if polygon:
        st.success("Polygon drawn.")
    if route_line:
        st.success("Route line drawn.")
    if start_point:
        st.success(f"Start point selected at ({start_point[0]:.5f}, {start_point[1]:.5f})")
    if end_point:
        st.success(f"End point selected at ({end_point[0]:.5f}, {end_point[1]:.5f})")

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

            # Filter out commercial/industrial/non-residential buildings
            buildings_gdf = buildings_gdf[~buildings_gdf["building"].isin([
                "commercial", "industrial", "retail", "garage", "service", "warehouse", "school", "university"
            ])]

            # --- Fallback: Add address-only features without building tags ---
            try:
                tags_addr_only = {
                    "addr:housenumber": True,
                    "addr:street": True,
                    "building": False  # exclude features tagged as buildings
                }
                addr_only_gdf = features_from_polygon(polygon, tags_addr_only)
                addr_only_gdf = addr_only_gdf[addr_only_gdf.geometry.is_valid]
                addr_only_gdf = addr_only_gdf[addr_only_gdf.geometry.type.isin(['Point', 'Polygon', 'MultiPolygon'])]

                if not buildings_gdf.empty:
                    addr_only_gdf = gpd.sjoin(addr_only_gdf, buildings_gdf, how="left", predicate='within', lsuffix='addr', rsuffix='bldg')
                    addr_only_gdf = addr_only_gdf[addr_only_gdf.index_bldg.isna()]
                    addr_only_gdf = addr_only_gdf.drop(columns=['index_bldg'])

                buildings_gdf = pd.concat([buildings_gdf, addr_only_gdf], ignore_index=True)
                st.info(f"Added {len(addr_only_gdf)} address-only elements without building outlines to results.")
            except Exception as e:
                st.warning(f"Failed to add address-only fallback data: {e}")

            if len(buildings_gdf) > 99:
                st.warning("Capping building count to 99 for performance.")
                buildings_gdf = buildings_gdf.head(99)

            st.success(f"{len(buildings_gdf)} building/structure footprints loaded.")

        except Exception as e:
            st.error(f"Error pulling OSM data: {e}")
            buildings_gdf = None

if buildings_gdf is not None and not buildings_gdf.empty:
    with st.spinner("Processing and geocoding buildings..."):
        addr_list = []
        reverse_address_list = []
        name_list = []
        comment_list = []
        flag_list = []
        note_list = []

        geolocator = Nominatim(user_agent="sidewalksort")
        geocode = RateLimiter(geolocator.reverse, min_delay_seconds=1)

        hn_col = "addr:housenumber" if "addr:housenumber" in buildings_gdf.columns else None
        st_col = "addr:street" if "addr:street" in buildings_gdf.columns else None

        st.markdown("Resolving addresses...")
        address_cache = {}
        total = len(buildings_gdf)
        progress = st.progress(0)

        turfcut_id = generate_turfcut_id()

        for i, (_, row) in enumerate(buildings_gdf.iterrows()):
            geom = row.geometry
            addr = ""
            if hn_col:
                addr += str(row[hn_col]) + " " if pd.notna(row[hn_col]) else ""
            if st_col:
                addr += str(row[st_col]) if pd.notna(row[st_col]) else ""
            addr = addr.strip()

            resolved = ""
            reverse_addr = ""
            flag = ""
            note = ""
            member_name = ""
            comment = ""

            try:
                # Try matching to CSV if available
                if addr and df is not None:
                    match, score, _ = process.extractOne(addr, df["address"], score_cutoff=85) or (None, None, None)
                    if match:
                        row_match = df[df["address"] == match].iloc[0]
                        resolved = match
                        member_name = row_match["member name"]
                        comment = row_match["comment"]
                        flag = "match"
                        note = "Matched to CSV"
                    else:
                        resolved = addr  # keep OSM address anyway
                        flag = "osm_only"
                        note = "No CSV match"
                else:
                    resolved = addr if addr else ""
                    # Reverse geocode fallback
                    key = f"{geom.centroid.y:.5f},{geom.centroid.x:.5f}"
                    if key in address_cache:
                        reverse_addr = address_cache[key]
                    else:
                        location = geocode((geom.centroid.y, geom.centroid.x))
                        reverse_addr = location.address if location else "Unknown"
                        address_cache[key] = reverse_addr
                    flag = "reverse"
                    note = "Reverse geocoded"

            except Exception as e:
                resolved = addr if addr else "Error processing"
                reverse_addr = ""
                note = f"Error: {e}"
                flag = "error"

            addr_list.append(resolved)
            reverse_address_list.append(reverse_addr)
            name_list.append(member_name)
            comment_list.append(comment)
            flag_list.append(flag)
            note_list.append(note)

            progress.progress((i + 1) / total)

        progress.progress(1.0)

        buildings_gdf["address"] = addr_list
        buildings_gdf["reverse_address"] = reverse_address_list
        buildings_gdf["member name"] = name_list
        buildings_gdf["comment"] = comment_list
        buildings_gdf["flag"] = flag_list
        buildings_gdf["note"] = note_list

        # --- Routing Order ---
        # If route line drawn, order buildings by projected distance along route line
        # Else fallback to numeric order (e.g., by centroid lat then lon)
        if route_line:
            def project_onto_route(pt):
                return route_line.project(pt)

            buildings_gdf["order_along_route"] = buildings_gdf.geometry.centroid.apply(project_onto_route)
            buildings_gdf = buildings_gdf.sort_values("order_along_route")
        else:
            buildings_gdf = buildings_gdf.sort_values(by=["address"])  # fallback alphabetical

        # Display results table
        st.markdown(f"### 🧾 Final Address Table — Turf Cut ID: {turfcut_id}")
        display_cols = ["address", "reverse_address", "member name", "comment", "flag", "note"]
        st.dataframe(buildings_gdf[display_cols], height=400)

        # Download CSV button
        csv_data = buildings_gdf[display_cols].to_csv(index=False)
        st.download_button(
            label=f"📥 Download CSV ({turfcut_id})",
            data=csv_data,
            file_name=f"{turfcut_id}_results.csv",
            mime="text/csv"
        )

# Footer
st.markdown("---")
st.caption("© 2025 Lucy Zentgraf. All rights reserved.")
