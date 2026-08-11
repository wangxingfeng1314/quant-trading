"""全局深色金融主题 CSS — 美化 Streamlit 默认观感

风格：深蓝黑底 + 红涨绿跌（A股配色）
用法：在 main.py 中调用 inject_theme_css() 一次即可全局生效。
"""
import streamlit as st

# A股配色
COLOR_UP = "#ef5350"      # 红（涨）
COLOR_DOWN = "#26a69a"    # 绿（跌）
COLOR_ACCENT = "#ffd54f"  # 金黄强调
COLOR_BG = "#0a0e17"
COLOR_BG_CARD = "#131c30"
COLOR_BORDER = "#1e2a44"

# 侧边栏深度美化
SIDEBAR_EXTRA = """
<style>
    /* ===== 侧边栏 ===== */
    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0d1526 0%, #0a0e17 100%);
        border-right: 1px solid #1e2a44;
    }
    section[data-testid="stSidebar"] .stRadio > div[role="radiogroup"] > label {
        padding: 8px 12px;
        border-radius: 8px;
        transition: all .15s ease;
        font-weight: 500;
    }
    section[data-testid="stSidebar"] .stRadio > div[role="radiogroup"] > label:hover {
        background: rgba(239, 83, 80, 0.08);
    }
    section[data-testid="stSidebar"] .stRadio > div[role="radiogroup"] > label:has(input:checked) {
        background: rgba(239, 83, 80, 0.12);
        border-left: 3px solid #ef5350;
        font-weight: 600;
    }
    section[data-testid="stSidebar"] h1, section[data-testid="stSidebar"] h2, section[data-testid="stSidebar"] h3 {
        color: #ffd54f;
    }
</style>
"""

# 主区域 + 组件美化
MAIN_EXTRA = """
<style>
    /* ===== 主背景 ===== */
    .stApp {
        background: radial-gradient(1200px 600px at 80% -10%, rgba(239,83,80,0.06), transparent),
                    radial-gradient(1000px 500px at 0% 0%, rgba(38,166,154,0.05), transparent),
                    #0a0e17;
    }
    [data-testid="stHeader"] { background: transparent; }

    /* ===== 卡片容器 ===== */
    .st-key-card, div[data-testid="stVerticalBlockBorderWrapper"] {
        background: #131c30;
        border: 1px solid #1e2a44;
        border-radius: 12px;
        padding: 4px 8px;
        box-shadow: 0 4px 16px rgba(0,0,0,0.25);
    }

    /* ===== 指标卡片 ===== */
    [data-testid="stMetric"] {
        background: linear-gradient(160deg, #131c30 0%, #0f1728 100%);
        border: 1px solid #1e2a44;
        border-radius: 12px;
        padding: 14px 16px;
        box-shadow: 0 4px 16px rgba(0,0,0,0.25);
        transition: transform .15s ease, border-color .15s ease;
    }
    [data-testid="stMetric"]:hover {
        transform: translateY(-2px);
        border-color: rgba(239,83,80,0.5);
    }
    [data-testid="stMetric"] label {
        color: #8b98b8 !important;
        font-size: 0.85rem !important;
    }
    [data-testid="stMetricValue"] {
        color: #e8edf7 !important;
        font-weight: 700;
        font-size: 1.35rem !important;
    }
    [data-testid="stMetricDelta"] { font-size: 0.85rem !important; }
    [data-testid="stMetricDelta"] [data-testid="stMetricDeltaIcon"] { display: none; }

    /* ===== 按钮 ===== */
    .stButton > button, .stDownloadButton > button {
        border-radius: 8px;
        border: 1px solid #2a3a5e;
        background: linear-gradient(180deg, #182338 0%, #131c30 100%);
        color: #dbe2ee;
        font-weight: 500;
        transition: all .15s ease;
    }
    .stButton > button:hover {
        border-color: #ef5350;
        color: #ef5350;
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(239,83,80,0.2);
    }
    .stButton > button[kind="primary"] {
        background: linear-gradient(180deg, #ef5350 0%, #d32f2f 100%);
        border: none;
        color: #ffffff;
    }
    .stButton > button[kind="primary"]:hover {
        box-shadow: 0 4px 16px rgba(239,83,80,0.4);
        color: #ffffff;
    }

    /* ===== 标签页 ===== */
    .stTabs [data-baseweb="tab-list"] {
        gap: 6px;
        background: transparent;
        border-bottom: 1px solid #1e2a44;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px 8px 0 0;
        padding: 8px 18px;
        color: #8b98b8;
        transition: all .15s ease;
    }
    .stTabs [data-baseweb="tab"]:hover { color: #dbe2ee; }
    .stTabs [aria-selected="true"] {
        background: rgba(239,83,80,0.10) !important;
        color: #ef5350 !important;
        border-bottom: 2px solid #ef5350 !important;
        font-weight: 600;
    }

    /* ===== 输入控件 ===== */
    .stTextInput input, .stNumberInput input, .stSelectbox [data-baseweb="select"] > div,
    .stDateInput [data-baseweb="input"], .stMultiSelect [data-baseweb="select"] > div,
    .stTextArea textarea {
        background-color: #0d1526 !important;
        border-color: #2a3a5e !important;
        color: #dbe2ee !important;
        border-radius: 8px;
    }
    .stTextInput input:focus, .stNumberInput input:focus { border-color: #ef5350 !important; }

    /* ===== 滑块 ===== */
    [data-testid="stSlider"] [data-baseweb="slider"] div[role="slider"] {
        background: #ef5350;
        border: 2px solid #ff8a80;
    }

    /* ===== 展开器 ===== */
    [data-testid="stExpander"] {
        background: #0d1526;
        border: 1px solid #1e2a44;
        border-radius: 10px;
    }

    /* ===== 警告/成功/错误 ===== */
    [data-testid="stAlert"] { border-radius: 10px; border-left-width: 4px; }
    [data-testid="stAlert"] [data-testid="stCaptionContainer"] { color: #dbe2ee; }

    /* ===== 进度条 ===== */
    [data-testid="stProgress"] > div > div > div { background: linear-gradient(90deg, #ef5350, #ffd54f); }

    /* ===== 表格 ===== */
    [data-testid="stDataFrame"] {
        border: 1px solid #1e2a44;
        border-radius: 10px;
        overflow: hidden;
    }
    [data-testid="stDataFrame"] thead tr th {
        background: #131c30 !important;
        color: #8b98b8 !important;
        font-weight: 600;
        border-bottom: 1px solid #2a3a5e !important;
    }

    /* ===== 滚动条 ===== */
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: #0a0e17; }
    ::-webkit-scrollbar-thumb { background: #2a3a5e; border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: #ef5350; }

    /* ===== 分割线 ===== */
    hr { border-color: #1e2a44 !important; }

    /* ===== 标题点缀 ===== */
    h1, h2, h3 { color: #e8edf7; letter-spacing: 0.3px; }
    h1 { border-bottom: 2px solid rgba(239,83,80,0.4); padding-bottom: 8px; }

    /* ===== 移动端响应式适配 ===== */
    @media (max-width: 768px) {
        /* 表格横向滚动 */
        [data-testid="stDataFrame"] { overflow-x: auto; -webkit-overflow-scrolling: touch; }
        /* 指标卡片缩小间距 */
        [data-testid="stMetric"] { padding: 8px 10px; }
        [data-testid="stMetricValue"] { font-size: 1.1rem !important; }
        /* 图表高度自适应 */
        .stPlotlyChart, .js-plotly-plot { height: 300px !important; }
        /* 侧边栏收窄 */
        section[data-testid="stSidebar"] { min-width: 200px; }
        /* 标题缩小 */
        h1 { font-size: 1.5rem; }
        h2 { font-size: 1.25rem; }
        /* 按钮全宽 */
        .stButton > button { width: 100%; }
    }
</style>
"""


def inject_theme_css():
    """注入全局主题 CSS（main.py 调用一次）"""
    st.markdown(SIDEBAR_EXTRA, unsafe_allow_html=True)
    st.markdown(MAIN_EXTRA, unsafe_allow_html=True)


def card_html(title: str, content_html: str, accent: str = "#ef5350") -> str:
    """生成深色卡片 HTML（用于 st.markdown 渲染）"""
    return f"""
    <div style="background:linear-gradient(160deg,#131c30,#0f1728);
                border:1px solid #1e2a44;border-left:3px solid {accent};
                border-radius:12px;padding:14px 18px;margin:8px 0;
                box-shadow:0 4px 16px rgba(0,0,0,0.25);">
        <div style="color:#ffd54f;font-weight:600;font-size:0.95rem;margin-bottom:6px;">{title}</div>
        <div style="color:#dbe2ee;font-size:0.92rem;line-height:1.6;">{content_html}</div>
    </div>
    """


def updown_color(value: float) -> str:
    """涨跌配色：A股红涨绿跌"""
    if value > 0:
        return COLOR_UP
    if value < 0:
        return COLOR_DOWN
    return "#dbe2ee"
