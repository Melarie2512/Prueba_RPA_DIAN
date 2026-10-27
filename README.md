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
- Google Chrome instalado (navegador por defecto del RPA; se cambia en `config.json` → `rpa.navegador`: `chrome`, `msedge` o `firefox`).
- Tesseract OCR con idioma español (`spa`). Solo hace falta si los PDF no traen capa de texto; las facturas de esta prueba sí la traen. En Windows: instalador de UB-Mannheim; si no queda en el PATH, define `TESSERACT_CMD` con la ruta a `tesseract.exe`.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/Mac: source .venv/bin/activate)
pip install -r requirements.txt
playwright install firefox      # solo si se usa rpa.navegador = "firefox"
```

## Variables de entorno

| Variable | Uso |
|---|---|
| `PDF_PASSWORD` | Contraseña de apertura de los PDF. La DIAN los entrega cifrados con el número de documento del adquiriente. Admite varias separadas por coma. Nunca se escribe en el código ni en `config.json`. |
| `TESSERACT_CMD` | Ruta a `tesseract.exe`, solo si se usa OCR y no está en el PATH. |

## Ejecución

```bash
python src/rpa_dian.py          # 1. descarga los PDF en data/pdf/

# Windows PowerShell:  $env:PDF_PASSWORD="<documento del adquiriente>"
# Linux/Mac:           export PDF_PASSWORD="<documento del adquiriente>"
python src/extraer_facturas.py  # 2. genera los CSV en data/csv/ y las métricas
```

Los dos scripts terminan con código `0` si todo salió bien y `1` si algo falló, para que un orquestador (n8n, cron, Task Scheduler) detecte el error.

## Captcha (Cloudflare Turnstile): hallazgo

El formulario de la DIAN está protegido con Cloudflare Turnstile. El RPA abre el navegador en ventana visible y espera el token de validación (`cf-turnstile-response`); si Turnstile pide verificación, avisa al operador para que marque la casilla.

**Resultado en la ejecución (26/10/2026):** Cloudflare rechazó la verificación ("La verificación falló") en los tres navegadores probados (Chromium de Playwright, Google Chrome y Firefox), **incluso con la casilla marcada por una persona**. Turnstile detecta que el navegador está controlado por automatización y lo bloquea por diseño. No se implementaron técnicas para evadir ese control.

Por eso los 10 PDF de la prueba se descargaron con consulta manual en un navegador normal, y la extracción (punto 2) se ejecutó de forma automática sobre ellos. Para un proceso productivo sin intervención, la vía recomendada es un canal autorizado: los servicios web de facturación electrónica de la DIAN para quien es parte de la factura, o la descarga desde el proveedor tecnológico.

El código del RPA queda completo (navegación, digitación del CUFE, envío, descarga, validación del PDF, reintentos y métricas) y funcionaría tal cual en un entorno donde el captcha valide al navegador.

## Resultados de la extracción (10 facturas)

| Indicador | Valor |
|---|---|
| Facturas procesadas | 10 de 10 (todas con capa de texto: no se requirió OCR) |
| Campos de encabezado extraídos | 30 de 30 |
| Productos extraídos | 21 |
| Celdas completas | 98,6 % (la única vacía es un código que la factura no trae) |
| Tiempo total / promedio por factura | 9,2 s / 0,92 s |

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
