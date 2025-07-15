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
uploaded_csv = st.file_uploade
