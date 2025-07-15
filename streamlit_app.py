import streamlit as st
import geopandas as gpd
import pandas as pd
import osmnx as ox
from osmnx.features import features_from_polygon
from shapely.geometry import Polygon, Point
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import folium
from folium import plugins
from streamlit_folium import st_folium
import networkx as nx
from networkx.algorithms.approximation import traveling_salesman_problem
from datetime import datetime
from rapidfuzz import process
import time

# --- Page Setup ---
st.set_page_config(layout="wide")
st.title("OCEAN Demo v.0.04.01")

if "turfcut_counter" not in st.session_state:
    st.session_state.turfcut_counter = 1

def generate_turfcut_id(number=None):
    year_code = datetime.now().strftime("%y")
    if number is None:
        number = st.session_state.turfcut_counter
        st.session_state.turfcut_counter += 1
    return f"TcID{year_code}{number:04d}"

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
                st.warning("No buildings found. Trying structure fallback...")
                tags = {"man_made": "building"}
                buildings_gdf = features_from_polygon(polygon, tags)
                buildings_gdf = buildings_gdf[buildings_gdf.geometry.is_valid]
                buildings_gdf = buildings_gdf[buildings_gdf.geometry.type.isin(['Polygon', 'MultiPolygon'])]
                buildings_gdf['geometry'] = buildings_gdf['geometry'].apply(simplify_geom)
                buildings_gdf = buildings_gdf[buildings_gdf['geometry'].notnull()]
                buildings_gdf = buildings_gdf[~buildings_gdf['geometry'].is_empty]
            st.success(f"{len(buildings_gdf)} building/structure footprints loaded.")

            if len(buildings_gdf) > 99:
                st.warning("Capping building count to 99 for performance.")
                buildings_gdf = buildings_gdf.head(99)

        except Exception as e:
            st.error(f"Error pulling OSM data: {e}")
            buildings_gdf = None

if buildings_gdf is not None and not buildings_gdf.empty:
    with st.spinner("Processing and geocoding buildings..."):
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

            try:
                if df is not None:
                    matched_address, score, idx = process.extractOne(addr, df["address"], score_cutoff=85) or (None, None, None)
                    matched_row = df[df["address"] == matched_address].head(1) if matched_address else pd.DataFrame()
                    if not matched_row.empty:
                        member_name = matched_row["member name"].values[0]
                        comment = matched_row["comment"].values[0]

                if addr and df is not None and not matched_row.empty:
                    resolved = addr
                    flag = "match"
                    note = "Matched from OSM"
                elif addr:
                    key = f"{geom.centroid.y:.5f},{geom.centroid.x:.5f}"
                    if key in address_cache:
                        resolved = address_cache[key]
                    else:
                        if geom.is_empty:
                            resolved = "Invalid geometry"
                        else:
                            with st.spinner("Reverse geocoding address..."):
                                loc = geocode((geom.centroid.y, geom.centroid.x))
                                resolved = loc.address if loc else "Unknown"
                        address_cache[key] = resolved
                    flag = "reverse"
                    note = "Reverse geocoded"
                else:
                    resolved = "No address"
                    flag = "error"
                    note = "No data or error"
            except Exception as e:
                resolved = f"Fallback: {addr if addr else 'N/A'}"
                flag = "fallback"
                note = f"Fallback triggered: {e}"

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
    with st.spinner("Loading pedestrian network and calculating optimal route..."):
        try:
            progress = st.progress(0, text="Routing: Starting...")
            G = ox.graph_from_polygon(polygon, network_type='walk')
            progress.progress(0.3, text="Routing: Pedestrian network loaded.")

            centroids = buildings_gdf.geometry.centroid
            node_ids = [ox.distance.nearest_nodes(G, pt.x, pt.y) for pt in centroids]
            if start_point:
                start_node = ox.distance.nearest_nodes(G, start_point[1], start_point[0])
                node_ids.insert(0, start_node)

            progress.progress(0.6, text="Routing: Solving TSP...")
            tsp_path = traveling_salesman_problem(G, node_ids, cycle=loop_back)
            progress.progress(0.9, text="Routing: Plotting route map...")

            route_map = ox.plot_route_folium(G, tsp_path, popup_attribute='name', tiles='cartodbpositron')
            st.markdown("### Turfcut Route Map")
            st_data = st_folium(route_map, width=800, height=600)

            turfcut_ids = [generate_turfcut_id(st.session_state.turfcut_counter + i) for i in range(len(buildings_gdf))]
            buildings_gdf["Turfcut ID"] = turfcut_ids
            st.session_state.turfcut_counter += len(buildings_gdf)

            st.markdown(f"#### Turfcut ID: {turfcut_ids[0]} - {turfcut_ids[-1]}")
            progress.progress(1.0, text="Routing: Complete")

        except Exception as e:
            st.error(f"Routing failed: {e}")

# Footer
st.markdown("---")
st.caption("© 2025 Lucy Zentgraf. All rights reserved.")
