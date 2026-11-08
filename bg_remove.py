import streamlit as st
from rembg import remove
from PIL import Image, ImageFilter, ImageOps
import numpy as np
from scipy.ndimage import binary_fill_holes, label
from io import BytesIO
import os
import traceback
import time

# --------------------------------------------------
# 1. Streamlit 페이지 기본 설정
# --------------------------------------------------
st.set_page_config(
    layout="wide",
    page_title="Image Background Remover"
)

st.write("## Remove background from your image")
st.write(
    ":dog: Upload an image to remove its background. "
    "Black outlines are protected and the final image "
    "is converted to grayscale."
)

st.sidebar.write("## Upload and download :gear:")

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_IMAGE_SIZE = 2000             # 최대 가로/세로 픽셀

# --------------------------------------------------
# 2. 사이드바 미세 조정 옵션
# --------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.write("### Fine-tuning Options :art:")

protect_all_lines = st.sidebar.checkbox(
    "잘린 외곽선 및 내부 영역 강제 복구",
    value=True,
    help="AI가 배경으로 오인해 지운 영역이나 멀리 떨어진 검은 외곽선을 복구합니다."
)
black_threshold = st.sidebar.slider(
    "검은색 선 인식 기준 (높을수록 옅은 선도 포함)",
    min_value=30, max_value=150, value=95, step=5
)
outer_gray_cutoff = st.sidebar.slider(
    "외곽선 밖 흐린 회색 배경 제거 기준 (낮출수록 더 진한 회색도 삭제)",
    min_value=120, max_value=255, value=200, step=5,
    help="검은 외곽선 바깥쪽에 남은 희미한 밑그림이나 회색 얼룩을 투명하게 지웁니다."
)
min_object_size = st.sidebar.slider(
    "작은 잡티/서명 제거 크기 (픽셀 수)",
    min_value=0, max_value=5000, value=1000, step=100,
    help="우측 하단 서명이나 배경의 작은 점들을 지울 때 값을 높여주세요."
)
alpha_cleanup = st.sidebar.slider(
    "배경 반투명 잔상 제거 강도",
    min_value=0, max_value=150, value=40, step=5
)
convert_to_gray = st.sidebar.checkbox("흑백(Grayscale) 변환", value=True)
contrast_boost = st.sidebar.checkbox("흑백 대비 선명하게 보정", value=True)


# --------------------------------------------------
# 3. 유틸리티 함수 (변환 및 리사이즈)
# --------------------------------------------------
def convert_image(img: Image.Image) -> bytes:
    """PIL 이미지를 PNG 바이트 데이터로 변환"""
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def resize_image(image: Image.Image, max_size: int = 2000) -> Image.Image:
    """비율을 유지하면서 최대 크기 제한"""
    width, height = image.size
    scale = min(max_size / width, max_size / height, 1.0)
    new_width = max(1, int(width * scale))
    new_height = max(1, int(height * scale))

    if (new_width, new_height) == (width, height):
        return image.copy()

    return image.resize((new_width, new_height), Image.Resampling.LANCZOS)


# --------------------------------------------------
# 4. 핵심 이미지 처리 파이프라인
# --------------------------------------------------
@st.cache_data(show_spinner=False)
def process_image(
    image_bytes: bytes,
    protect_all: bool,
    black_thresh: int,
    outer_cutoff: int,
    min_size: int,
    alpha_thresh: int,
    to_gray: bool,
    boost_contrast: bool
):
    try:
        # [단계 1] 원본 이미지 로드 및 복사
        original = Image.open(BytesIO(image_bytes)).convert("RGBA")
        original_copy = original.copy()

        # [단계 2] 처리 속도와 메모리 보호를 위한 크기 제한
        processing_image = resize_image(original, MAX_IMAGE_SIZE)

        # [단계 3] rembg AI 배경 제거
        input_buffer = BytesIO()
        processing_image.save(input_buffer, format="PNG")
        output_bytes = remove(input_buffer.getvalue())

        # [단계 4] 배경 제거 결과 로드 및 해상도 동기화
        fixed = Image.open(BytesIO(output_bytes)).convert("RGBA")
        if fixed.size != processing_image.size:
            fixed = fixed.resize(processing_image.size, Image.Resampling.LANCZOS)

        width, height = fixed.size

        # [단계 5] 원본 이미지 기준 그레이스케일 및 검은색 외곽선 감지
        white_bg = Image.new("RGBA", processing_image.size, (255, 255, 255, 255))
        source_on_white = Image.alpha_composite(white_bg, processing_image).convert("RGB")
        original_gray = np.array(ImageOps.grayscale(source_on_white))

        black_bool = original_gray < black_thresh
        filled_regions = binary_fill_holes(black_bool)

        # [단계 6] 기본 Alpha 채널 정리 및 외곽선 밖 흐린 배경 제거
        alpha_np = np.array(fixed.getchannel("A"))
        alpha_np = np.where(alpha_np < alpha_thresh, 0, alpha_np).astype(np.uint8)

        is_outside_outline = ~filled_regions
        is_faint_gray = original_gray >= outer_cutoff
        alpha_np = np.where(is_outside_outline & is_faint_gray, 0, alpha_np).astype(np.uint8)

        # [단계 7] 외곽선 및 잘린 파츠 내부 영역 복구
        if protect_all:
            combined_mask = (alpha_np > 0) | filled_regions

            # 독립된 작은 덩어리(작가 서명, 노이즈 등) 필터링
            if min_size > 0:
                labeled_array, num_features = label(combined_mask)
                if num_features > 0:
                    sizes = np.bincount(labeled_array.ravel())
                    mask_sizes = sizes >= min_size
                    mask_sizes[0] = False  # 0번 인덱스(전체 배경)는 항상 제외
                    combined_mask = mask_sizes[labeled_array]

            # 필터링 후 탈락한 작은 잡티를 alpha_np에서도 제거
            alpha_np = np.where(combined_mask, alpha_np, 0).astype(np.uint8)

            # 내부는 불투명도 255로 채우고, 외곽 경계선만 안티앨리어싱 처리
            recovered_alpha = np.where(combined_mask, 255, 0).astype(np.uint8)
            dilated_alpha = Image.fromarray(recovered_alpha).filter(ImageFilter.MaxFilter(3))
            smooth_edge = dilated_alpha.filter(ImageFilter.GaussianBlur(radius=0.6))

            clean_recovered = np.where(combined_mask, 255, np.array(smooth_edge)).astype(np.uint8)
            final_alpha = np.maximum(alpha_np, clean_recovered)
        else:
            clean_alpha = Image.fromarray(alpha_np)
            expanded_alpha = clean_alpha.filter(ImageFilter.MaxFilter(15))
            expanded_np = np.array(expanded_alpha)
            protected_lines = np.where(
                black_bool & (expanded_np > 20), 255, 0
            ).astype(np.uint8)
            final_alpha = np.maximum(alpha_np, protected_lines)

        # [단계 8] 색상/흑백 변환 및 RGBA 채널 결합
        result_np = np.zeros((height, width, 4), dtype=np.uint8)

        if to_gray:
            grayscale = ImageOps.grayscale(source_on_white)
            if boost_contrast:
                grayscale = ImageOps.autocontrast(grayscale, cutoff=2)
            grayscale_np = np.array(grayscale)
            result_np[:, :, 0] = grayscale_np
            result_np[:, :, 1] = grayscale_np
            result_np[:, :, 2] = grayscale_np
        else:
            source_rgb_np = np.array(source_on_white)
            result_np[:, :, :3] = source_rgb_np

        result_np[:, :, 3] = final_alpha

        # [단계 9] 최종 이미지 생성
        result = Image.fromarray(result_np, "RGBA")
        return original_copy, result

    except Exception:
        print("IMAGE PROCESSING ERROR")
        traceback.print_exc()
        raise


# --------------------------------------------------
# 5. UI 실행 및 결과 출력 로직
# --------------------------------------------------
def fix_image(upload):
    try:
        start_time = time.time()
        progress_bar = st.sidebar.progress(0)
        status_text = st.sidebar.empty()

        status_text.text("Loading image...")
        progress_bar.progress(10)

        if isinstance(upload, str):
            if not os.path.exists(upload):
                st.error(f"Image not found: {upload}")
                return
            with open(upload, "rb") as f:
                image_bytes = f.read()
            base_name = os.path.splitext(os.path.basename(upload))[0]
        else:
            image_bytes = upload.getvalue()
            base_name = os.path.splitext(upload.name)[0]

        status_text.text("Removing background...")
        progress_bar.progress(30)

        image, fixed = process_image(
            image_bytes,
            protect_all_lines,
            black_threshold,
            outer_gray_cutoff,
            min_object_size,
            alpha_cleanup,
            convert_to_gray,
            contrast_boost
        )

        progress_bar.progress(80)
        status_text.text("Displaying results...")

        col1.write("Original Image :camera:")
        col2.write("Fixed Image :wrench:")

        caption_text = "Background Removed - Grayscale" if convert_to_gray else "Background Removed - Color"

        with col1:
            st.image(image, width=400, caption="Original Image")

        with col2:
            st.image(fixed, width=400, caption=caption_text)

        st.sidebar.markdown("---")
        st.sidebar.download_button(
            label="Download fixed image",
            data=convert_image(fixed),
            file_name=f"{base_name}_cleaned.png",
            mime="image/png"
        )

        progress_bar.progress(100)
        processing_time = time.time() - start_time
        status_text.text(f"Completed in {processing_time:.2f} seconds")

    except Exception as e:
        st.error(f"Image processing failed: {str(e)}")
        st.error("Please check the terminal for detailed errors.")
        print(traceback.format_exc())


# --------------------------------------------------
# 6. 메인 레이아웃 구성
# --------------------------------------------------
col1, col2 = st.columns(2, gap="large")

my_upload = st.sidebar.file_uploader(
    "Upload an image",
    type=["png", "jpg", "jpeg"]
)

with st.sidebar.expander("Image Guidelines"):
    st.write("""
    - **Maximum file size:** 10MB
    - **Maximum processing dimension:** 2000px
    - **Supported formats:** PNG, JPG, JPEG
    - **Black outlines** are protected & filled
    - **Transparent background** is preserved
    """)

if my_upload is not None:
    if my_upload.size > MAX_FILE_SIZE:
        st.error("Please upload an image smaller than 10MB.")
    else:
        fix_image(my_upload)
else:
    default_images = ["./zebra.jpg", "./wallaby.png"]
    for img_path in default_images:
        if os.path.exists(img_path):
            fix_image(img_path)
            break
    else:
        st.info("Please upload an image to get started!")