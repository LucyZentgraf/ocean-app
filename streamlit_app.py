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
from datetime import datetime

# --- Page Setup ---
st.set_page_config(layout="wide")
st.title("OCEAN Demo v.0.03.00")

# --- Turfcut ID Generator (simple in-memory counter for demo) ---
if "turfcut_counter" not in st.session_state:
    st.session_state.turfcut_counter = 1

def generate_turfcut_id():
    year_code = datetime.now().strftime("%y")
    number = f"{st.session_state.turfcut_counter:04d}"
    return f"TcID{year_code}{number}"

# --- Step 1: CSV Upload ---
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

# --- Step 2: Draw Turf ---
st.markdown("Draw Turf")
with st.expander("Click to draw polygon"):
    m = folium.Map(location=[40.7128, -74.006], zoom_start=13)
    draw = folium.plugins.Draw(export=True)
    draw.add_to(m)
    output = st_folium(m, width=700, height=500, returned_objects=["last_active_drawing"])

# --- Step 3: Get Buildings ---
polygon = None
buildings_gdf = None
if output and output.get("last_active_drawing"):
    coords = output["last_active_drawing"]["geometry"]["coordinates"][0]
    polygon = Polygon([(pt[0], pt[1]) for pt in coords])
    st.success("Polygon drawn.")

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

# --- Step 4: Address Extraction and Geocoding ---
if buildings_gdf is not None and not buildings_gdf.empty:
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
    progress_bar = st.progress(0)
    status_text = st.empty()

    final_data = []
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

        final_data.append({
            "flag": flag,
            "Address": resolved,
            "member name": member_name,
            "Comment": comment,
            "result": note
        })

        progress_bar.progress((i + 1) / total)
        status_text.text(f"Processed {i + 1}/{total}")

    progress_bar.empty()
    status_text.text("Done!")

    final_df = pd.DataFrame(final_data).drop_duplicates()

    # --- Step 5: Routing ---
    st.markdown("🧭 Routing")
    geocoded_points = []
    routing_bar = st.progress(0)
    route_status = st.empty()

    for i, row in enumerate(final_df.itertuples()):
        if "error" in row.Address.lower() or row.Address == "Unknown":
            continue
        try:
            loc = geolocator.geocode(row.Address)
            if loc:
                geocoded_points.append((loc.latitude, loc.longitude, row.Address))
        except Exception as e:
            pass
        routing_bar.progress((i + 1) / len(final_df))

    routing_bar.empty()

    if len(geocoded_points) < 2:
        st.warning("Not enough valid geocoded points to create a route.")
    else:
        fallback = False
        try:
            graph = ox.graph_from_polygon(polygon, network_type='walk')
            node_ids = [ox.distance.nearest_nodes(graph, x=lon, y=lat) for lat, lon, _ in geocoded_points]
            route = []
            for i in range(len(node_ids) - 1):
                segment = nx.shortest_path(graph, node_ids[i], node_ids[i+1], weight='length')
                route.extend(segment if i == 0 else segment[1:])
            route_map = folium.Map(location=[geocoded_points[0][0], geocoded_points[0][1]], zoom_start=15)
            ox.plot_route_folium(graph, route, route_map, color='blue', weight=4)
            for i, (lat, lon, addr) in enumerate(geocoded_points):
                folium.Marker([lat, lon], tooltip=f"{i+1}. {addr}").add_to(route_map)
            st_folium(route_map, width=800, height=500)
        except Exception as e:
            fallback = True
            st.warning(f"Routing failed, falling back to linear address order: {e}")
            fallback_map = folium.Map(location=[geocoded_points[0][0], geocoded_points[0][1]], zoom_start=15)
            folium.PolyLine([(lat, lon) for lat,])
# --- Step 6: Output final turfcut table ---
turfcut_id = generate_turfcut_id()
st.markdown(f"### Turfcut ID: {turfcut_id}")

# Vertical (transposed) display of the table
def vertical_table(df):
    for idx, row in df.iterrows():
        st.markdown(f"---\n**Record {idx+1}**")
        for col in df.columns:
            st.write(f"**{col}**: {row[col]}")

vertical_table(final_df)

# Download button
csv_data = final_df.to_csv(index=False)
st.download_button(
    label="Download Turf Log CSV",
    data=csv_data,
    file_name=f"{turfcut_id}_turf_log.csv",
    mime="text/csv"
)
# --- Footer ---
st.markdown("---")
st.markdown("Demo made for NYPIRG FUND by Lucy Zentgraf, 2025. All Rights Reserved")


# Increment Turfcut ID counter for next run
st.session_state.turfcut_counter += 1
