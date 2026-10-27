# RPA DIAN: descarga y extracción de facturas electrónicas

Solución a la prueba técnica de Analista de Innovación 2026.

1. **`src/rpa_dian.py`**: RPA (Playwright) que consulta cada CUFE en el catálogo de la DIAN y descarga el PDF de la representación gráfica de la factura.
2. **`src/extraer_facturas.py`**: convierte cada PDF a imagen, obtiene el texto (capa nativa del PDF u OCR con Tesseract), extrae los campos pedidos y exporta un CSV por factura, un consolidado y las métricas del proceso.

## Estructura

```
config/config.json          única palanca de configuración (CUFE, rutas, tiempos)
src/rpa_dian.py             punto 1: RPA de descarga
src/extraer_facturas.py     punto 2: extracción y exportación a CSV
data/                       salidas locales (no se versiona)
  pdf/                      PDFs descargados (<cufe>.pdf)
  imagenes/                 páginas exportadas a PNG
  csv/                      un CSV por factura + facturas_consolidado.csv
  metricas_rpa.csv          estado y tiempos por CUFE
  metricas_extraccion.csv   tiempos por etapa, método, confianza OCR, completitud
  proceso.log               bitácora de ambos scripts
docs/                       documento de respuesta de la prueba (Word)
```

## Requisitos

- Python 3.12
- Tesseract OCR con idioma español (`spa`). Solo hace falta si los PDF no traen capa de texto. En Windows: instalador de UB-Mannheim; si no queda en el PATH, define `TESSERACT_CMD` con la ruta a `tesseract.exe`.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/Mac: source .venv/bin/activate)
pip install -r requirements.txt
playwright install chromium
```

## Ejecución

```bash
python src/rpa_dian.py          # 1. descarga los PDF en data/pdf/
python src/extraer_facturas.py  # 2. genera los CSV en data/csv/ y las métricas
```

Los dos scripts terminan con código `0` si todo salió bien y `1` si algo falló, para que un orquestador (n8n, cron, Task Scheduler) detecte el error.

## Captcha (Cloudflare Turnstile)

El formulario de la DIAN está protegido con Cloudflare Turnstile. El RPA abre el navegador en ventana visible y espera el token de validación (`cf-turnstile-response`):

- Si Turnstile valida el navegador por sí solo, el flujo continúa sin intervención (`metodo_captcha = auto`).
- Si pide verificación, la bitácora avisa al operador, que marca la casilla en la ventana; el RPA continúa en cuanto detecta el token (`metodo_captcha = asistido`).

Todo lo demás (navegación, digitación del CUFE, envío, descarga, validación del PDF, reintentos y métricas) es automático.

## Campos extraídos

| Sección del PDF | Campo | Columna CSV |
|---|---|---|
| Datos del Documento | Número de factura | `numero_factura` |
| Datos del Documento | Fecha de emisión | `fecha_emision` |
| Datos del Emisor / Vendedor | NIT del emisor | `nit_emisor` |
| Detalles de Productos | Código | `codigo` |
| Detalles de Productos | Descripción | `descripcion` |
| Detalles de Productos | Cantidad | `cantidad` |
| Detalles de Productos | Precio unitario | `precio_unitario` |

Cada CSV tiene un renglón por producto, con los datos del encabezado repetidos.

## Métricas

- `metricas_rpa.csv`: `estado` (descargado / no_encontrado / error), `intentos`, `metodo_captcha`, `seg_captcha`, `seg_total`, `bytes`.
- `metricas_extraccion.csv`: `metodo_texto` (nativo / ocr), `confianza_ocr_media`, `seg_exportar_imagenes`, `seg_obtener_texto`, `seg_interpretar`, `seg_total`, `campos_encabezado_ok` (de 3), `n_productos`, `pct_celdas_completas`.
