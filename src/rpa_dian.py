"""
rpa_dian.py - RPA que descarga la representacion grafica (PDF) de facturas
electronicas desde el catalogo publico de la DIAN, a partir de su CUFE.

Pagina objetivo: https://catalogo-vpfe.dian.gov.co/User/SearchDocument

Que hace
--------
1. Lee la lista de CUFE y los parametros desde config/config.json.
2. Por cada CUFE abre la pagina de busqueda en Chromium (Playwright), en ventana visible.
3. Espera a que el captcha Cloudflare Turnstile quede validado. Turnstile suele validar
   solo a un navegador normal; cuando pide interaccion, el operador marca la casilla
   en la ventana (modo asistido) y el RPA continua en cuanto detecta el token.
4. Escribe el CUFE en el campo "CUFE o UUID" (id DocumentKey) y envia el formulario.
5. En la pagina del documento ubica el enlace de descarga del PDF y lo guarda en
   data/pdf/<cufe>.pdf, validando que el archivo sea realmente un PDF.
6. Registra estado y tiempos de cada CUFE en data/metricas_rpa.csv.

Como se ejecuta
---------------
    pip install -r requirements.txt
    playwright install chromium
    python src/rpa_dian.py

Codigo de salida
----------------
    0 -> todos los CUFE se procesaron (descargado o "no_encontrado" en la DIAN).
    1 -> al menos un CUFE fallo por un error tecnico (captcha sin validar, red, descarga).
"""

# ---------------------------------------------------------------------------
# Importaciones
# ---------------------------------------------------------------------------
import csv           # escritura del archivo de metricas
import json          # lectura de config/config.json
import logging       # bitacora en consola y en archivo
import random        # pausas aleatorias entre consultas (no saturar el sitio)
import sys           # codigo de salida para orquestadores (n8n, cron, etc.)
import time          # medicion de tiempos
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
# Raiz del proyecto: este archivo vive en <raiz>/src/, por eso subimos dos niveles.
RAIZ = Path(__file__).resolve().parent.parent

# Selectores de la pagina de busqueda, verificados contra el HTML publico de la DIAN
# (formulario "search-document-form").
SEL_CAMPO_CUFE = "#DocumentKey"                # input "CUFE o UUID"
SEL_BOTON_BUSCAR = "button.search-document"    # boton "Buscar"
NOMBRE_CAMPO_TOKEN = "cf-turnstile-response"   # input oculto donde Turnstile deja su token

# Selectores de la pagina de resultado. Se dejan varias alternativas porque esa
# pagina solo es visible despues de validar el captcha y su HTML puede cambiar.
SEL_ENLACE_PDF = (
    "a[href*='DownloadPDF'], "
    "a:has-text('Descargar PDF'), "
    "a:has-text('Descargar representación')"
)
# Mensajes que la DIAN muestra cuando el CUFE no existe o es invalido.
SEL_ERROR = (
    "#errorModal .modal-body:visible, "
    ".field-validation-error, "
    ".alert-danger:visible"
)

log = logging.getLogger("rpa_dian")


# ---------------------------------------------------------------------------
# Utilidades de configuracion y bitacora
# ---------------------------------------------------------------------------
def cargar_config() -> dict:
    """Lee config/config.json y lo devuelve como diccionario."""
    with open(RAIZ / "config" / "config.json", encoding="utf-8") as f:
        return json.load(f)


def configurar_log(ruta_log: Path) -> None:
    """Envia la bitacora a consola y a archivo, con fecha y nivel en cada linea."""
    ruta_log.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(ruta_log, encoding="utf-8"),
        ],
    )


# ---------------------------------------------------------------------------
# Captcha: Cloudflare Turnstile (modo asistido)
# ---------------------------------------------------------------------------
def leer_token(page: Page) -> str:
    """Devuelve el token de Turnstile si ya fue emitido; cadena vacia si no."""
    return page.evaluate(
        f"() => document.querySelector('[name=\"{NOMBRE_CAMPO_TOKEN}\"]')?.value || ''"
    )


def esperar_captcha(page: Page, segundos: float) -> str:
    """
    Espera a que Turnstile quede validado y devuelve como se valido:
      "auto"     -> el token aparecio en los primeros 5 s, sin intervencion.
      "asistido" -> hizo falta que el operador marcara la casilla en la ventana.
    Lanza TimeoutError si no hay token dentro del plazo configurado.
    """
    inicio = time.monotonic()
    avisado = False
    while time.monotonic() - inicio < segundos:
        if leer_token(page):
            return "auto" if not avisado else "asistido"
        # A los 5 s sin token se avisa una sola vez al operador.
        if not avisado and time.monotonic() - inicio > 5:
            log.info("   Turnstile pide verificacion: marca la casilla en la ventana del navegador.")
            avisado = True
        page.wait_for_timeout(500)
    raise TimeoutError("Turnstile no se valido dentro del plazo")


# ---------------------------------------------------------------------------
# Consulta y descarga de un CUFE
# ---------------------------------------------------------------------------
def guardar_pdf(page: Page, destino: Path, timeout_ms: int) -> int:
    """
    Descarga el PDF desde la pagina del documento y devuelve su tamano en bytes.
    Primero intenta la descarga normal del navegador; si el enlace abre el PDF en
    pantalla en lugar de descargarlo, lo pide por HTTP reutilizando la sesion
    (page.request comparte las cookies del navegador).
    """
    enlace = page.locator(SEL_ENLACE_PDF).first
    try:
        with page.expect_download(timeout=timeout_ms) as info:
            enlace.click()
        info.value.save_as(destino)
    except PlaywrightTimeout:
        href = enlace.get_attribute("href")
        resp = page.request.get(urljoin(page.url, href), timeout=timeout_ms)
        destino.write_bytes(resp.body())

    # Validacion: todo PDF empieza con la firma "%PDF". Si no, es una pagina de error.
    contenido = destino.read_bytes()
    if not contenido.startswith(b"%PDF"):
        destino.unlink(missing_ok=True)
        raise RuntimeError("El archivo descargado no es un PDF")
    return len(contenido)


def procesar_cufe(page: Page, cufe: str, cfg: dict, carpeta_pdf: Path) -> dict:
    """
    Ejecuta el flujo completo para un CUFE y devuelve un diccionario con el resultado.
    Estados posibles: "descargado" o "no_encontrado" (la DIAN no reconoce el CUFE).
    Cualquier otra situacion se propaga como excepcion para que el llamador reintente.
    """
    timeout_ms = cfg["rpa"]["timeout_descarga_seg"] * 1000
    t0 = time.monotonic()

    # 1. Abrir la pagina de busqueda limpia (cada consulta necesita un token nuevo).
    page.goto(cfg["url_busqueda"], wait_until="domcontentloaded", timeout=timeout_ms)

    # 2. Esperar la validacion del captcha y medir cuanto tarda.
    t_cap = time.monotonic()
    metodo = esperar_captcha(page, cfg["rpa"]["timeout_captcha_seg"])
    seg_captcha = time.monotonic() - t_cap

    # 3. Escribir el CUFE y enviar el formulario.
    page.fill(SEL_CAMPO_CUFE, cufe)
    page.click(SEL_BOTON_BUSCAR)

    # 4. Esperar lo primero que aparezca: el enlace del PDF o un mensaje de error.
    page.locator(SEL_ENLACE_PDF).or_(page.locator(SEL_ERROR)).first.wait_for(timeout=timeout_ms)

    resultado = {"cufe": cufe, "metodo_captcha": metodo, "seg_captcha": round(seg_captcha, 2)}

    if page.locator(SEL_ENLACE_PDF).count() == 0:
        # La DIAN respondio pero no encontro el documento: no es un error tecnico.
        resultado.update(estado="no_encontrado", archivo="", bytes=0,
                         detalle=page.locator(SEL_ERROR).first.inner_text().strip()[:200])
    else:
        # 5. Descargar y validar el PDF.
        destino = carpeta_pdf / f"{cufe}.pdf"
        tamano = guardar_pdf(page, destino, timeout_ms)
        resultado.update(estado="descargado", archivo=str(destino.relative_to(RAIZ)),
                         bytes=tamano, detalle="")

    resultado["seg_total"] = round(time.monotonic() - t0, 2)
    return resultado


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------
def main() -> int:
    """Recorre todos los CUFE con reintentos, escribe metricas y devuelve el codigo de salida."""
    cfg = cargar_config()
    rutas = cfg["rutas"]
    configurar_log(RAIZ / rutas["log"])
    carpeta_pdf = RAIZ / rutas["pdf"]
    carpeta_pdf.mkdir(parents=True, exist_ok=True)

    resultados = []
    inicio_lote = time.monotonic()

    with sync_playwright() as pw:
        # Ventana visible: el operador debe poder ver y validar el captcha si se lo piden.
        navegador = pw.chromium.launch(headless=False)
        contexto = navegador.new_context(accept_downloads=True, locale="es-CO")
        page = contexto.new_page()

        for n, cufe in enumerate(cfg["cufes"], start=1):
            log.info("(%d/%d) CUFE %s...", n, len(cfg["cufes"]), cufe[:16])
            fila = None

            # Reintentos con espera creciente (5 s, 10 s, ...) ante fallos tecnicos.
            for intento in range(1, cfg["rpa"]["reintentos_por_cufe"] + 1):
                try:
                    fila = procesar_cufe(page, cufe, cfg, carpeta_pdf)
                    fila["intentos"] = intento
                    log.info("   -> %s en %.1f s (captcha: %s)",
                             fila["estado"], fila["seg_total"], fila["metodo_captcha"])
                    break
                except Exception as e:  # se registra y se reintenta cualquier fallo
                    log.warning("   intento %d fallo: %s", intento, e)
                    fila = {"cufe": cufe, "estado": "error", "intentos": intento,
                            "metodo_captcha": "", "seg_captcha": 0, "seg_total": 0,
                            "archivo": "", "bytes": 0, "detalle": str(e)[:200]}
                    time.sleep(5 * intento)

            resultados.append(fila)
            # Pausa aleatoria entre consultas para no generar trafico en rafaga.
            time.sleep(random.uniform(*cfg["rpa"]["pausa_entre_cufes_seg"]))

        navegador.close()

    # Escribir metricas por CUFE.
    columnas = ["cufe", "estado", "intentos", "metodo_captcha", "seg_captcha",
                "seg_total", "archivo", "bytes", "detalle"]
    ruta_met = RAIZ / rutas["metricas_rpa"]
    with open(ruta_met, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columnas)
        w.writeheader()
        w.writerows(resultados)

    # Resumen del lote en la bitacora.
    descargados = sum(r["estado"] == "descargado" for r in resultados)
    errores = sum(r["estado"] == "error" for r in resultados)
    log.info("Lote terminado en %.1f s: %d descargados, %d no encontrados, %d errores",
             time.monotonic() - inicio_lote, descargados,
             len(resultados) - descargados - errores, errores)
    log.info("Metricas en %s", ruta_met)

    # Fallar ruidoso: codigo 1 si hubo errores tecnicos.
    return 1 if errores else 0


if __name__ == "__main__":
    sys.exit(main())
