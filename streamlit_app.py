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
st.title("OCEAN Demo v.0.05.02")

# Turf Cut ID generator with session state counter
if "turfcut_counter" not in st.session_state:
    st.session_state.turfcut_counter = 1

def generate_turfcut_id(number=None):
    year_code = datetime.now().strftime("%y")
    if number is None:
        number = st.session_state.turfcut_counter
        st.session_state.turfcut_counter += 1
    return f"TcID{year_code}{number:04d}"

# --- Step 1: Optional CSV Upload ---
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

st.markdown("Draw Turf Area and Set Route")

# --- Step 2: Draw Polygon, Route, Start/End Markers ---
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
                start_point = (coords[1], coords[0])
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

# --- Step 3: Pull Building Footprints from OSM ---
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

            # Exclude commercial/non-residential building types
            exclude_buildings = ["commercial", "industrial", "retail", "garage", "service", "warehouse", "school", "university"]
            buildings_gdf = buildings_gdf[~buildings_gdf["building"].isin(exclude_buildings)]

            # Fallback to man_made buildings if none found
            if buildings_gdf.empty:
                st.warning("No buildings found. Trying structure fallback...")
                tags = {"man_made": "building"}
                buildings_gdf = features_from_polygon(polygon, tags)
                buildings_gdf = buildings_gdf[buildings_gdf.geometry.is_valid]
                buildings_gdf = buildings_gdf[buildings_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]
                buildings_gdf['geometry'] = buildings_gdf['geometry'].apply(simplify_geom)
                buildings_gdf = buildings_gdf[buildings_gdf['geometry'].notnull()]
                buildings_gdf = buildings_gdf[~buildings_gdf['geometry'].is_empty]

            # Cap building count at 99
            if len(buildings_gdf) > 99:
                st.warning("Capping building count to 99 for performance.")
                buildings_gdf = buildings_gdf.head(99)

            st.success(f"{len(buildings_gdf)} building/structure footprints loaded.")

        except Exception as e:
            st.error(f"Error pulling OSM data: {e}")
            buildings_gdf = None

# --- Step 4: Process Buildings and Match/Geocode Addresses ---
if buildings_gdf is not None and not buildings_gdf.empty:

    with st.spinner("Processing and geocoding buildings..."):
        flag_list = []
        address_list = []
        reverse_address_list = []
        member_name_list = []
        comment_list = []
        result_list = []

        geolocator = Nominatim(user_agent="sidewalksort")
        geocode = RateLimiter(geolocator.reverse, min_delay_seconds=1)

        # Compose OSM addresses if possible
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

        # Cache to reduce reverse geocode calls
        address_cache = {}
        progress_bar = st.progress(0, text="Geocoding: 0%")
        total = len(buildings_gdf)

        for i, (_, row) in enumerate(buildings_gdf.iterrows()):
            geom = row.geometry
            addr = row.get("osm_address", "")
            flag = ""
            resolved = ""
            note = ""
            member_name = ""
            comment = ""

            try:
                matched_row = pd.DataFrame()
                if df is not None and addr:
                    matched_address, score, idx = process.extractOne(addr, df["address"], score_cutoff=85) or (None, None, None)
                    matched_row = df[df["address"] == matched_address].head(1) if matched_address else pd.DataFrame()
                    if not matched_row.empty:
                        member_name = matched_row["member name"].values[0]
                        comment = matched_row["comment"].values[0]

                if addr and df is not None and not matched_row.empty:
                    resolved = addr
                    flag = "match"
                    note = "Matched from CSV"
                elif addr and addr.strip() != "":
                    # Try to reverse geocode from geometry centroid
                    key = f"{geom.centroid.y:.5f},{geom.centroid.x:.5f}"
                    if key in address_cache:
                        resolved = address_cache[key]
                    else:
                        if geom.is_empty or not isinstance(geom.centroid, Point):
                            resolved = "Invalid geometry"
                        else:
                            loc = geocode((geom.centroid.y, geom.centroid.x))
                            resolved = loc.address if loc else "Unknown"
                        address_cache[key] = resolved
                    flag = "reverse"
                    note = "Reverse geocoded"
                else:
                    # No building outline address, fallback to OSM nodes with addr:* tags inside polygon
                    resolved = "No address"
                    flag = "error"
                    note = "No address data or building outline"

            except Exception as e:
                resolved = f"Fallback: {addr if addr else 'N/A'}"
                flag = "fallback"
                note = f"Exception: {e}"

            flag_list.append(flag)
            address_list.append(resolved)
            reverse_address_list.append(addr)
            member_name_list.append(member_name)
            comment_list.append(comment)
            result_list.append(note)

            percent_complete = int(((i + 1) / total) * 100)
            progress_bar.progress((i + 1) / total, text=f"Geocoding: {percent_complete}%")

        progress_bar.progress(1.0, text="Geocoding: 100%")

        buildings_gdf["address"] = address_list
        buildings_gdf["reverse_address"] = reverse_address_list
        buildings_gdf["member name"] = member_name_list
        buildings_gdf["comment"] = comment_list
        buildings_gdf["flag"] = flag_list
        buildings_gdf["note"] = result_list

        # --- Additional pass: include OSM addresses inside polygon without building outlines ---
        try:
            tags_addr = {"addr:housenumber": True, "addr:street": True}
            nodes_with_addr = features_from_polygon(polygon, tags_addr)
            # Remove duplicates already in buildings_gdf
            if nodes_with_addr is not None and not nodes_with_addr.empty:
                # Compose full address
                nodes_with_addr["full_addr"] = nodes_with_addr["addr:housenumber"].fillna("") + " " + nodes_with_addr["addr:street"].fillna("")
                nodes_with_addr["full_addr"] = nodes_with_addr["full_addr"].str.strip()
                # Keep those not in buildings_gdf
                existing_addrs = set(buildings_gdf["address"].dropna().tolist() + buildings_gdf["reverse_address"].dropna().tolist())
                filtered_nodes = nodes_with_addr[~nodes_with_addr["full_addr"].isin(existing_addrs)]
                # Append to buildings_gdf for processing
                if not filtered_nodes.empty:
                    # Create a GeoDataFrame with relevant columns and dummy flags
                    extra_df = pd.DataFrame({
                        "address": filtered_nodes["full_addr"],
                        "reverse_address": filtered_nodes["full_addr"],
                        "member name": ["" for _ in range(len(filtered_nodes))],
                        "comment": ["" for _ in range(len(filtered_nodes))],
                        "flag": ["osm_node" for _ in range(len(filtered_nodes))],
                        "note": ["OSM address node fallback" for _ in range(len(filtered_nodes))],
                        "geometry": filtered_nodes.geometry
                    })
                    extra_gdf = gpd.GeoDataFrame(extra_df, geometry="geometry", crs=buildings_gdf.crs)
                    buildings_gdf = pd.concat([buildings_gdf, extra_gdf], ignore_index=True)
        except Exception as e:
            st.warning(f"Error during fallback address node extraction: {e}")

        # --- Route Sorting ---

        def fallback_sort(gdf):
            # Simple fallback: sort by proximity to start point or polygon centroid if no route line
            if start_point:
                start_pt = Point(start_point[1], start_point[0])  # lat/lon reversed
            else:
                start_pt = polygon.centroid if polygon else None
            if start_pt is None:
                return gdf
            gdf["distance"] = gdf.geometry.centroid.distance(start_pt)
            return gdf.sort_values("distance")

        # Sort by route line projection if route_line exists, else fallback sort
        if route_line is not None:
            try:
                def project_onto_route(pt):
                    return route_line.project(pt)
                buildings_gdf["order_along_route"] = buildings_gdf.geometry.centroid.apply(project_onto_route)
                buildings_gdf = buildings_gdf.sort_values("order_along_route")
            except Exception as e:
                st.warning(f"Route sorting failed, falling back: {e}")
                buildings_gdf = fallback_sort(buildings_gdf)
        else:
            buildings_gdf = fallback_sort(buildings_gdf)

        # --- Remove duplicate addresses based on combined address columns ---
        buildings_gdf['unique_addr'] = buildings_gdf.apply(
            lambda row: row['address'] if row['address'] and row['address'] != '' else row['reverse_address'],
            axis=1
        )
        buildings_gdf = buildings_gdf.drop_duplicates(subset=['unique_addr'], keep='first')

        # --- Split unique address into street number and name ---
        def split_address(addr):
            if not addr or addr == '':
                return '', ''
            parts = addr.split()
            if parts[0].isdigit():
                number = parts[0]
                street = " ".join(parts[1:]) if len(parts) > 1 else ''
            else:
                number = ''
                street = addr
            return number, street

        buildings_gdf[['Street Number', 'Street Name']] = buildings_gdf['unique_addr'].apply(
            lambda x: pd.Series(split_address(x))
        )

        # --- Compose 'Result' column ---
        buildings_gdf['Result'] = buildings_gdf.apply(lambda row: f"{row['flag']}: {row['note']}", axis=1)

        # --- Columns to display and export ---
        display_cols = ['Street Number', 'Street Name', 'member name', 'comment', 'Result']

        # Generate Turf Cut ID
        turfcut_id = generate_turfcut_id()

        st.markdown(f"### 🧾 Final Turf Log — Turf Cut ID: {turfcut_id}")
        st.dataframe(buildings_gdf[display_cols], height=400)

        # Prepare CSV data
        csv_data = buildings_gdf[display_cols].to_csv(index=False)

        st.download_button(
            label=f"📥 Download Turf Log CSV ({turfcut_id})",
            data=csv_data,
            file_name=f"{turfcut_id}_turf_log.csv",
            mime="text/csv"
        )

# --- Footer ---
st.markdown("---")
st.caption("© 2025 Lucy Zentgraf. All rights reserved.")
