import streamlit as st
import geopandas as gpd
import pandas as pd
import osmnx as ox
from osmnx.features import features_from_polygon
import networkx as nx
from shapely.geometry import Point, Polygon
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import folium
from streamlit_folium import st_folium

# --- Page Setup ---
st.set_page_config(layout="wide")
st.title("OCEAN Demo")

# --- Step 1: Optional CSV Upload ---
uploaded_csv = st.file_uploader("Upload CSV with member data (column: 'address')", type="csv")
df = None
if uploaded_csv:
    df = pd.read_csv(uploaded_csv)
    if 'address' not in df.columns:
        st.error("CSV must have a column named 'address'")
    else:
        st.success(f"Loaded {len(df)} addresses.")
        st.markdown("### 📄 Uploaded Address Table")
        st.dataframe(df, height=300)

# --- Step 2: Draw Map Turf ---
st.markdown("Draw Turf")
with st.expander("Click to draw polygon"):
    m = folium.Map(location=[40.7128, -74.006], zoom_start=13)
    draw = folium.plugins.Draw(export=True)
    draw.add_to(m)
    output = st_folium(m, width=700, height=500, returned_objects=["last_active_drawing"])

# --- Step 3: Download OSM Building Data ---
polygon = None
buildings_gdf = None

if output and output.get("last_active_drawing"):
    coords = output["last_active_drawing"]["geometry"]["coordinates"][0]
    polygon = Polygon([(pt[0], pt[1]) for pt in coords])
    st.success("Polygon drawn.")

if polygon:
    st.markdown("Review Data")
    try:
        tags = {"building": True}
        buildings_gdf = features_from_polygon(polygon, tags)
        buildings_gdf = buildings_gdf[buildings_gdf.geometry.type == 'Polygon']
        buildings_gdf = buildings_gdf[~buildings_gdf["building"].isin([
            "commercial", "industrial", "retail", "garage", "service", "warehouse", "school", "university"
        ])]
        st.success(f"Downloaded {len(buildings_gdf)} filtered residential-like building footprints.")
    except Exception as e:
        st.error(f"Error pulling OSM data: {e}")

# --- Step 4: Extract or Match Addresses ---
if buildings_gdf is not None and not buildings_gdf.empty:
    st.markdown("Addresses")
    geolocator = Nominatim(user_agent="sidewalksort")
    geocode = RateLimiter(geolocator.reverse, min_delay_seconds=1)

    def get_address(geom):
        try:
            location = geocode((geom.centroid.y, geom.centroid.x))
            return location.address if location else "Unknown"
        except:
            return "Unknown"

    buildings_gdf["osm_address"] = buildings_gdf["addr:housenumber"].fillna("") + " " + buildings_gdf["addr:street"].fillna("")
    buildings_gdf["osm_address"] = buildings_gdf["osm_address"].str.strip()

    matched, reverse, unknown = [], [], []

    for _, row in buildings_gdf.iterrows():
        addr = row["osm_address"]
        if df is not None and addr in df["address"].values:
            matched.append(addr)
        elif not addr or addr.strip() == "":
            rev = get_address(row.geometry)
            reverse.append(rev)
        else:
            unknown.append(addr)

    st.markdown(f"**Matched:** {len(matched)} | **Reverse-Geocoded:** {len(reverse)} | **Unknown:** {len(unknown)}")

    # Combine all addresses for display
    all_addresses = pd.DataFrame({
        "Address": matched + reverse + unknown
    }).drop_duplicates()

    # --- Step 5: Routing Stub (placeholder for future sidewalk-safe logic) ---
    st.markdown("Route")
    st.info("Routing based on pedestrian-safe sidewalk logic will be implemented in the next version. This version lists all extracted addresses.")

    # --- Step 6: Final Table + Download ---
    st.markdown("Turf Log)
    st.dataframe(all_addresses, height=400)

    st.download_button(
        label="Downloadv Turf Log",
        data=all_addresses.to_csv(index=False),
        file_name="ordered_addresses.csv",
        mime="text/csv"
    )

# --- Footer ---
st.markdown("---")
st.markdown("Demo made for NYPIRG FUND by Lucy Zentgraf, 2025. All Rights Reserved")
