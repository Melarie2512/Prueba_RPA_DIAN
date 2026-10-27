"""
extraer_facturas.py - Extrae los datos de las facturas descargadas por rpa_dian.py
y los exporta a CSV (un DataFrame por archivo + un consolidado).

Que hace
--------
1. Recorre los PDF de data/pdf/.
2. Exporta cada pagina a imagen PNG (data/imagenes/) a la resolucion configurada.
3. Obtiene el texto de cada factura:
     - modo "auto": usa la capa de texto del PDF si existe (rapido y sin errores de
       lectura) y recurre a OCR (Tesseract) sobre las imagenes solo si el PDF es escaneado.
     - modo "ocr": fuerza OCR sobre las imagenes siempre.
4. De "Datos del Documento" y "Datos del Emisor/Vendedor" extrae:
       numero_factura, fecha_emision, nit_emisor
   De la tabla "Detalles de Productos" extrae por renglon:
       codigo, descripcion, cantidad, precio_unitario
5. Construye un DataFrame por archivo (un renglon por producto, con los datos del
   encabezado repetidos) y lo guarda en data/csv/<archivo>.csv.
6. Guarda data/csv/facturas_consolidado.csv y las metricas del proceso en
   data/metricas_extraccion.csv (tiempos por etapa, metodo, confianza OCR, completitud).

Como se ejecuta
---------------
    pip install -r requirements.txt
    # Solo para OCR: instalar Tesseract con el idioma espanol (spa) y, si no esta en el
    # PATH, definir la variable de entorno TESSERACT_CMD con la ruta a tesseract.exe
    python src/extraer_facturas.py

Codigo de salida
----------------
    0 -> todos los PDF se procesaron.
    1 -> no habia PDF o al menos un archivo fallo.
"""

# ---------------------------------------------------------------------------
# Importaciones
# ---------------------------------------------------------------------------
import json
import logging
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import fitz                      # PyMuPDF: render de paginas a imagen, texto y tablas
import pandas as pd
import pytesseract               # envoltorio de Python para el motor OCR Tesseract
from PIL import Image

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
RAIZ = Path(__file__).resolve().parent.parent

# Columnas finales de cada DataFrame, en orden.
COLUMNAS = ["archivo", "numero_factura", "fecha_emision", "nit_emisor",
            "codigo", "descripcion", "cantidad", "precio_unitario"]

# Para ubicar las columnas de la tabla de productos se compara el encabezado de cada
# columna (normalizado: minusculas y sin tildes) con estas palabras clave.
# Gana la primera columna que coincida, por eso "precio unitario" toma la columna de
# precio unitario y no "precio unitario de venta", que aparece despues.
CLAVES_COLUMNAS = {
    "codigo": ["codigo", "cod."],
    "descripcion": ["descripcion"],
    "cantidad": ["cantidad", "cant."],
    "precio_unitario": ["precio unitario", "valor unitario", "vr. unitario", "precio unit"],
}

# Expresiones regulares del encabezado. Se aplican sobre texto normalizado (sin tildes,
# insensible a mayusculas), asi "Emisión" y "EMISION" se tratan igual.
RE_NUMERO = re.compile(
    r"(?:numero\s+de\s+factura|factura\s+electronica\s+de\s+venta(?:\s*(?:no\.?|n[°º]|#))?"
    r"|no\.?\s*factura)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]{2,})",
    re.IGNORECASE,
)
RE_FECHA = re.compile(
    r"fecha\s+de\s+emision\s*:?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
    re.IGNORECASE,
)
RE_NIT = re.compile(r"nit(?:\s+del\s+emisor)?\s*:?\s*([\d.\s]{6,15}(?:-\s*\d)?)", re.IGNORECASE)

# Renglon de producto leido por OCR (cuando no hay tabla nativa). Forma esperada:
#   [no.] CODIGO  DESCRIPCION ...  [U/M]  CANTIDAD  PRECIO_UNITARIO ...
RE_RENGLON_OCR = re.compile(
    r"^(?:\d{1,3}\s+)?(?P<codigo>[A-Za-z0-9][\w\-.]*)\s+(?P<descripcion>.+?)\s+"
    r"(?:[A-Za-z]{1,4}\s+)?(?P<cantidad>\d+(?:[.,]\d+)?)\s+\$?\s?(?P<precio>\d[\d.,]*)"
)
# Titulos que marcan el fin de la tabla de productos en el texto OCR.
FIN_TABLA = ("notas", "subtotal", "total bruto", "datos totales", "total a pagar")

log = logging.getLogger("extraer_facturas")


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def normalizar(texto: str) -> str:
    """Quita tildes (NFKD + descartar marcas combinantes) para comparar texto con regex simples."""
    sin_tildes = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in sin_tildes if not unicodedata.combining(c))


def a_numero(valor) -> float | None:
    """
    Convierte un importe en texto a float respetando el formato colombiano.
    Ejemplos: "1.234.567,89" -> 1234567.89 | "1,234.50" -> 1234.5 | "$ 12.000" -> 12000.0
    Regla: si hay punto y coma, el ultimo que aparece es el separador decimal; si hay uno
    solo, se trata como separador de miles cuando agrupa exactamente de a tres digitos.
    """
    if valor is None:
        return None
    s = re.sub(r"[^\d.,\-]", "", str(valor))
    if not s:
        return None
    if "." in s and "," in s:
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
        miles = "." if decimal == "," else ","
        s = s.replace(miles, "").replace(decimal, ".")
    elif "," in s:
        s = s.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", s) else s.replace(",", ".")
    elif "." in s and re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Etapa 1: PDF -> imagenes
# ---------------------------------------------------------------------------
def exportar_imagenes(pdf: fitz.Document, nombre: str, carpeta: Path, dpi: int) -> list[Path]:
    """Guarda cada pagina como PNG (<nombre>_p1.png, ...) y devuelve las rutas."""
    carpeta.mkdir(parents=True, exist_ok=True)
    rutas = []
    for i, pagina in enumerate(pdf, start=1):
        ruta = carpeta / f"{nombre}_p{i}.png"
        pagina.get_pixmap(dpi=dpi).save(ruta)
        rutas.append(ruta)
    return rutas


# ---------------------------------------------------------------------------
# Etapa 2: obtener texto (nativo u OCR)
# ---------------------------------------------------------------------------
def ocr_imagen(ruta: Path, idioma: str) -> tuple[str, list[float]]:
    """
    Aplica OCR a una imagen y devuelve (texto, confianzas por palabra).
    Se usa image_to_data en lugar de image_to_string para obtener en una sola pasada
    el texto y la confianza (0-100) de cada palabra. Las palabras se reagrupan en
    lineas con la llave (bloque, parrafo, linea) que entrega Tesseract.
    --psm 6 asume un bloque de texto uniforme, lo que conserva mejor las filas de tablas.
    """
    datos = pytesseract.image_to_data(
        Image.open(ruta), lang=idioma, config="--psm 6",
        output_type=pytesseract.Output.DICT,
    )
    lineas, confianzas = {}, []
    for i, palabra in enumerate(datos["text"]):
        if not palabra.strip():
            continue
        llave = (datos["block_num"][i], datos["par_num"][i], datos["line_num"][i])
        lineas.setdefault(llave, []).append(palabra)
        conf = float(datos["conf"][i])
        if conf >= 0:  # Tesseract marca con -1 los elementos que no son palabras
            confianzas.append(conf)
    texto = "\n".join(" ".join(p) for _, p in sorted(lineas.items()))
    return texto, confianzas


def obtener_texto(pdf: fitz.Document, imagenes: list[Path], cfg: dict) -> tuple[str, str, float | None]:
    """
    Devuelve (texto, metodo, confianza_media_ocr).
    En modo "auto" usa la capa de texto si tiene al menos N caracteres; si no, hace OCR.
    """
    ext = cfg["extraccion"]
    if ext["modo"] == "auto":
        nativo = "\n".join(p.get_text() for p in pdf)
        if len(nativo.strip()) >= ext["min_caracteres_texto_nativo"]:
            return nativo, "nativo", None

    textos, confs = [], []
    for img in imagenes:
        t, c = ocr_imagen(img, ext["idioma_ocr"])
        textos.append(t)
        confs.extend(c)
    media = round(sum(confs) / len(confs), 1) if confs else None
    return "\n".join(textos), "ocr", media


# ---------------------------------------------------------------------------
# Etapa 3: interpretar el texto
# ---------------------------------------------------------------------------
def extraer_encabezado(texto: str) -> dict:
    """
    Busca numero de factura, fecha de emision y NIT del emisor.
    El NIT se busca despues del titulo "Datos del Emisor" para no tomar por error el
    NIT del adquiriente; si ese titulo no aparece, se usa el primer NIT del documento.
    """
    t = normalizar(texto)
    m_num = RE_NUMERO.search(t)
    m_fecha = RE_FECHA.search(t)

    pos_emisor = t.lower().find("datos del emisor")
    m_nit = RE_NIT.search(t, pos_emisor if pos_emisor >= 0 else 0)

    return {
        "numero_factura": m_num.group(1) if m_num else None,
        "fecha_emision": m_fecha.group(1) if m_fecha else None,
        # Se dejan solo digitos y el guion del digito de verificacion.
        "nit_emisor": re.sub(r"[^\d-]", "", m_nit.group(1)) if m_nit else None,
    }


def mapear_columnas(encabezado: list) -> dict:
    """Devuelve {campo: indice_columna} comparando el encabezado con CLAVES_COLUMNAS."""
    celdas = [normalizar(str(c or "")).lower().replace("\n", " ") for c in encabezado]
    mapa = {}
    for campo, claves in CLAVES_COLUMNAS.items():
        for idx, celda in enumerate(celdas):
            if any(k in celda for k in claves) and idx not in mapa.values():
                mapa[campo] = idx
                break
    return mapa


def productos_desde_tablas(pdf: fitz.Document) -> list[dict]:
    """
    Lee la tabla de productos desde la estructura del PDF (PyMuPDF find_tables).
    Se queda con las tablas cuyo encabezado tenga al menos descripcion y cantidad.
    """
    productos = []
    for pagina in pdf:
        for tabla in pagina.find_tables().tables:
            filas = tabla.extract()
            if not filas:
                continue
            mapa = mapear_columnas(filas[0])
            if not {"descripcion", "cantidad"} <= mapa.keys():
                continue  # no es la tabla de productos
            for fila in filas[1:]:
                def celda(campo):
                    i = mapa.get(campo)
                    return (fila[i] or "").strip() if i is not None and i < len(fila) else None
                if not celda("descripcion"):
                    continue  # renglon vacio o de subtotal
                productos.append({
                    "codigo": celda("codigo"),
                    "descripcion": " ".join(celda("descripcion").split()),
                    "cantidad": a_numero(celda("cantidad")),
                    "precio_unitario": a_numero(celda("precio_unitario")),
                })
    return productos


def productos_desde_texto(texto: str) -> list[dict]:
    """
    Lee la tabla de productos desde texto plano (resultado del OCR).
    Toma las lineas entre el titulo "Detalles de Productos" y el siguiente bloque
    (notas, subtotales) y aplica RE_RENGLON_OCR a cada una.
    """
    lineas = normalizar(texto).splitlines()
    dentro, productos = False, []
    for linea in lineas:
        bajo = linea.lower().strip()
        if "detalle" in bajo and "producto" in bajo:
            dentro = True
            continue
        if not dentro or "descripcion" in bajo:  # fuera de la seccion o fila de titulos
            continue
        if bajo.startswith(FIN_TABLA):
            break
        m = RE_RENGLON_OCR.match(linea.strip())
        if m:
            productos.append({
                "codigo": m["codigo"],
                "descripcion": m["descripcion"].strip(),
                "cantidad": a_numero(m["cantidad"]),
                "precio_unitario": a_numero(m["precio"]),
            })
    return productos


# ---------------------------------------------------------------------------
# Proceso por archivo
# ---------------------------------------------------------------------------
def procesar_pdf(ruta_pdf: Path, cfg: dict) -> tuple[pd.DataFrame, dict]:
    """Procesa un PDF y devuelve (DataFrame de la factura, metricas del archivo)."""
    rutas = cfg["rutas"]
    t0 = time.monotonic()

    with fitz.open(ruta_pdf) as pdf:
        # Etapa 1: imagenes
        imagenes = exportar_imagenes(pdf, ruta_pdf.stem, RAIZ / rutas["imagenes"],
                                     cfg["extraccion"]["dpi"])
        t1 = time.monotonic()

        # Etapa 2: texto
        texto, metodo, confianza = obtener_texto(pdf, imagenes, cfg)
        t2 = time.monotonic()

        # Etapa 3: interpretacion. Con texto nativo se intenta primero la tabla
        # estructurada; si no se encuentra, o si vino de OCR, se parsea el texto.
        encabezado = extraer_encabezado(texto)
        productos = productos_desde_tablas(pdf) if metodo == "nativo" else []
        if not productos:
            productos = productos_desde_texto(texto)
        t3 = time.monotonic()
        paginas = len(pdf)

    # Si no se hallaron productos se deja una fila con el encabezado, para no perder la factura.
    filas = [{**encabezado, **p} for p in productos] or [encabezado]
    df = pd.DataFrame(filas)
    df.insert(0, "archivo", ruta_pdf.name)
    df = df.reindex(columns=COLUMNAS)

    metricas = {
        "archivo": ruta_pdf.name,
        "paginas": paginas,
        "metodo_texto": metodo,
        "confianza_ocr_media": confianza,
        "caracteres_texto": len(texto),
        "seg_exportar_imagenes": round(t1 - t0, 3),
        "seg_obtener_texto": round(t2 - t1, 3),
        "seg_interpretar": round(t3 - t2, 3),
        "seg_total": round(t3 - t0, 3),
        "campos_encabezado_ok": sum(v is not None for v in encabezado.values()),  # de 3
        "n_productos": len(productos),
        "pct_celdas_completas": round(100 * df[COLUMNAS[1:]].notna().mean().mean(), 1),
    }
    return df, metricas


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------
def main() -> int:
    """Procesa todos los PDF, exporta CSV y metricas, y devuelve el codigo de salida."""
    with open(RAIZ / "config" / "config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    rutas = cfg["rutas"]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(RAIZ / rutas["log"], encoding="utf-8")],
    )

    # Permite indicar la ruta de tesseract.exe cuando no esta en el PATH (comun en Windows).
    if os.getenv("TESSERACT_CMD"):
        pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]

    pdfs = sorted((RAIZ / rutas["pdf"]).glob("*.pdf"))
    if not pdfs:
        log.error("No hay PDF en %s. Ejecuta primero src/rpa_dian.py", rutas["pdf"])
        return 1

    carpeta_csv = RAIZ / rutas["csv"]
    carpeta_csv.mkdir(parents=True, exist_ok=True)
    dataframes, metricas, fallidos = [], [], 0

    for ruta in pdfs:
        try:
            df, met = procesar_pdf(ruta, cfg)
            # Un CSV por factura. utf-8-sig agrega la marca BOM para que Excel lea bien las tildes.
            df.to_csv(carpeta_csv / f"{ruta.stem}.csv", index=False, encoding="utf-8-sig")
            dataframes.append(df)
            metricas.append(met)
            log.info("%s: %s, %d productos, %d/3 campos, %.2f s",
                     ruta.name[:20], met["metodo_texto"], met["n_productos"],
                     met["campos_encabezado_ok"], met["seg_total"])
        except Exception as e:
            fallidos += 1
            log.error("%s fallo: %s", ruta.name, e)

    # Consolidado y metricas del lote.
    if dataframes:
        pd.concat(dataframes, ignore_index=True).to_csv(
            carpeta_csv / "facturas_consolidado.csv", index=False, encoding="utf-8-sig")
    met_df = pd.DataFrame(metricas)
    met_df.to_csv(RAIZ / rutas["metricas_extraccion"], index=False, encoding="utf-8-sig")

    if not met_df.empty:
        log.info("Resumen: %d archivos, %.2f s en total, %.2f s promedio por archivo",
                 len(met_df), met_df["seg_total"].sum(), met_df["seg_total"].mean())
    return 1 if fallidos else 0


if __name__ == "__main__":
    sys.exit(main())
