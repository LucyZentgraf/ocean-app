import streamlit as st
import geopandas as gpd
import pandas as pd
import osmnx as ox
from osmnx.features import features_from_polygon
from shapely.geometry import Polygon
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import folium
from streamlit_folium import st_folium
import networkx as nx
from networkx.algorithms.approximation import traveling_salesman_problem
from datetime import datetime

# --- Page Setup ---
st.set_page_config(layout="wide")
st.title("OCEAN Demo v.0.03.06")

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
    draw = folium.plugins.Draw(
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
        elif geom_type == "Point":
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
        buildings_gdf = buildings_gdf[buildings_gdf.geometry.type == 'Polygon']
        buildings_gdf = buildings_gdf[~buildings_gdf["building"].isin([
            "commercial", "industrial", "retail", "garage", "service", "warehouse", "school", "university"
        ])]
        if buildings_gdf.empty:
            st.warning("No buildings found. Trying structure fallback...")
            tags = {"man_made": "building"}
            buildings_gdf = features_from_polygon(polygon, tags)
            buildings_gdf = buildings_gdf[buildings_gdf.geometry.type == "Polygon"]
        st.success(f"{len(buildings_gdf)} building/structure footprints loaded.")
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
    total = len(buildings_gdf)
    for i, (_, row) in enumerate(buildings_gdf.iterrows()):
        geom = row.geometry
        addr = row["osm_address"]
        flag, resolved, note, member_name, comment = "", "", "", "", ""

        if df is not None:
            matched_row = df[df["address"] == addr].head(1)
            if not matched_row.empty:
                member_name = matched_row["member name"].values[0]
                comment = matched_row["comment"].values[0]

        if addr and df is not None and addr in df["address"].values:
            resolved = addr
            flag = "match"
            note = "Matched from OSM"
        elif addr:
            key = f"{geom.centroid.y:.5f},{geom.centroid.x:.5f}"
            if key in address_cache:
                resolved = address_cache[key]
            else:
                try:
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
        st.progress((i + 1) / total, text=f"Geocoding: {percent_complete}%")

    final_df = pd.DataFrame({
        "flag": flag_list,
        "Address": address_list,
        "member name": member_name_list,
        "Comment": comment_list,
        "result": result_list
    }).drop_duplicates()

    st.markdown("Route")
    geocoded_points = []
    for i, row in enumerate(final_df.itertuples()):
        if "error" in row.Address.lower() or row.Address == "Unknown":
            continue
        try:
            loc = geolocator.geocode(row.Address)
            if loc:
                geocoded_points.append((loc.latitude, loc.longitude, row.Address))
        except Exception:
            pass
        percent_complete = int(((i + 1) / len(final_df)) * 100)
        st.progress((i + 1) / len(final_df), text=f"Routing Prep: {percent_complete}%")

    if len(geocoded_points) < 2:
        st.warning("Not enough valid geocoded points. Generating fallback route...")
        fallback_points = [(p.y, p.x) for p in buildings_gdf.centroid.to_list()]
fallback_graph = ox.graph_from_polygon(polygon, network_type='walk')

valid_nodes = []
for pt in fallback_points:
    try:
        node = ox.distance.nearest_nodes(fallback_graph, x=pt[1], y=pt[0])
        valid_nodes.append(node)
    except Exception:
        continue

if len(valid_nodes) < 2:
    st.error("Fallback routing failed: Not enough valid nodes from centroid points.")
else:
    fallback_subG = fallback_graph.subgraph(valid_nodes)
    fallback_path = traveling_salesman_problem(fallback_subG, cycle=loop_back)
    route_map = folium.Map(location=start_point or fallback_points[0], zoom_start=15)
    route_coords = []
    for i in range(len(fallback_path)):
        u = fallback_path[i]
        v = fallback_path[(i + 1) % len(fallback_path)] if loop_back else fallback_path[i + 1] if i + 1 < len(fallback_path) else None
        if v:
            seg = nx.shortest_path(fallback_graph, u, v, weight="length")
            route_coords.extend(seg if not route_coords else seg[1:])
    ox.plot_route_folium(fallback_graph, route_coords, route_map, color='red', weight=4)
    st_folium(route_map, width=800, height=500)

        route_map = folium.Map(location=start_point or fallback_points[0], zoom_start=15)
        route_coords = []
        for i in range(len(fallback_path)):
            u = fallback_path[i]
            v = fallback_path[(i + 1) % len(fallback_path)] if loop_back else fallback_path[i + 1] if i + 1 < len(fallback_path) else None
            if v:
                seg = nx.shortest_path(fallback_graph, u, v, weight="length")
                route_coords.extend(seg if not route_coords else seg[1:])
        ox.plot_route_folium(fallback_graph, route_coords, route_map, color='red', weight=4)
        st_folium(route_map, width=800, height=500)
    else:
        try:
            G = ox.graph_from_polygon(polygon, network_type='walk')
            node_ids = [ox.distance.nearest_nodes(G, x=lon, y=lat) for lat, lon, _ in geocoded_points]
            subG = G.subgraph(node_ids)
            tsp_path = traveling_salesman_problem(subG, cycle=loop_back)

            route = []
            for i in range(len(tsp_path)):
                u = tsp_path[i]
                v = tsp_path[(i + 1) % len(tsp_path)] if loop_back else tsp_path[i + 1] if i + 1 < len(tsp_path) else None
                if v:
                    seg = nx.shortest_path(G, u, v, weight="length")
                    route.extend(seg if not route else seg[1:])

            route_map = folium.Map(location=[geocoded_points[0][0], geocoded_points[0][1]], zoom_start=15)
            ox.plot_route_folium(G, route, route_map, color='blue', weight=4)

            for i, (lat, lon, addr) in enumerate(geocoded_points):
                folium.Marker([lat, lon], tooltip=f"{i+1}. {addr}", icon=folium.Icon(color="green" if i == 0 and addr == "Start Point" else "blue")).add_to(route_map)

            st_folium(route_map, width=800, height=500)
        except Exception as e:
            st.warning(f"Routing failed: {e}")

    if 'final_df' in locals() and not final_df.empty:
        turfcut_id = generate_turfcut_id()
        st.markdown(f"### Turfcut ID: {turfcut_id}")

        def vertical_table(df):
            for idx, row in df.iterrows():
                st.markdown(f"---\n**Record {idx+1}**")
                for col in df.columns:
                    st.write(f"**{col}**: {row[col]}")

        vertical_table(final_df)

        csv_data = final_df.to_csv(index=False)
        st.download_button(
            label="Download Turf Log CSV",
            data=csv_data,
            file_name=f"{turfcut_id}_turf_log.csv",
            mime="text/csv"
        )
    else:
        st.warning("No turfcut data to display.")

st.markdown("---")
st.markdown("Demo made for NYPIRG FUND by Lucy Zentgraf, 2025. All Rights Reserved")
st.session_state.turfcut_counter += 1
