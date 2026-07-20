# borghese-monitor

Monitor automático de disponibilidad de entradas para la **Galleria Borghese** (Roma).
Cada 3 horas, un GitHub Action comprueba si ya se venden entradas para el
**25 de septiembre de 2026 por la tarde (15:00 o 17:00), 4 personas**, y si detecta
algo **abre un Issue** en este repo (GitHub envía email automáticamente al crearse
el Issue — esa es la notificación, sin SMTP ni tokens extra).

> ⚠️ El monitor **solo avisa, no compra**. Cuando llegue el aviso, entra tú a comprar
> con los links del Issue. La web oficial libera cada fecha con poca antelación y
> las plazas vuelan.

## Fuentes que revisa

1. **Oficial — tosc.it** (TicketOne Sistemi Culturali, red Eventim/Vivaticket)
2. **Respaldo — GetYourGuide** ([producto t468068](https://www.getyourguide.com/rome-l33/borghese-gallery-entry-ticket-and-audioguide-app-t468068/))

## Cómo detecta disponibilidad (selectores reales)

Descubiertos navegando los flujos reales de compra con Playwright (2026-07-20).
La documentación completa vive en comentarios al inicio de
[`scripts/check_borghese.py`](scripts/check_borghese.py).

### tosc.it — dos capas

- **API pública de Eventim** (señal principal, apta para CI):
  `GET https://public-api.eventim.com/websearch/search/api/exploration/v1/products?webId=web__ticketone-it&language=en&product_group_id=2253937`
  devuelve un "product" por cada día del museo a la venta, con `startDate` y
  `productId`. **Que el 25-09 aparezca en esa lista = la fecha está liberada.**
  Se usa la API porque el HTML de tosc.it está detrás de Akamai Bot Manager,
  que responde `Access Denied` a navegadores headless (comprobado); la API, en
  cambio, respondió sin cookies ni fingerprint especial.
- **DOM de la página del día** (`/en/event/galleria-borghese-galleria-borghese-<productId>/`),
  mejor esfuerzo para el detalle por turno:
  - una tarjeta por turno: `div[data-qa="price-category"]`, con el título del turno
    en `form[data-qa="pc-list-number-IN 3 pm-OUT 5 pm"]` (¡el sitio mezcla formatos
    12h/24h: "IN 5 pm-OUT 7 pm" pero "IN 18:00-OUT 20:00"!)
  - turno **agotado** → filas con `.ticket-type-unavailable-sec` ("Not available")
  - turno **disponible** → la tarjeta contiene el stepper de cantidad
    `.js-stepper` / `[data-qa="more-tickets"]` (detección por elemento, no por texto)

### GetYourGuide

- Parámetro de URL `?_pc=1,4` preselecciona 4 adultos. **Ojo:** `date_from` con una
  fecha fuera del horizonte reservable hace 404 la página entera, así que el
  monitor navega el datepicker en vez de confiar en la URL.
- Banner de cookies (Usercentrics): botón "Let's go" / "Accept all".
- Abrir calendario: `button.gtm-trigger__adp-date-picker-interaction`
  (por clase — el label cambia cuando hay fecha elegida).
- Cambiar de mes: `button.c-datepicker-month__arrow`.
- Día del calendario: `.c-datepicker-day__container` con
  `aria-label="Friday, September 25, 2026"` y `aria-disabled="false"` si es
  reservable (los no disponibles llevan además `c-datepicker-day--disabled`).
- Turnos tras elegir día: sección `.starting-times__layout`, chips `button.c-chip`
  con horas tipo "3:00 PM" (se filtran por patrón de hora), y aviso de escasez
  `.badge-label` ("Only 7 spots left").

### Calibración: avisar de más

Mejor un falso positivo que perder el cupo. Se abre Issue si **cualquiera** de las
fuentes muestra la fecha a la venta, aunque no se pueda confirmar el turno exacto
de 15:00/17:00 ni el cupo para 4; el Issue detalla qué se pudo confirmar y qué no.
Para no llenar el correo, si ya hay un Issue abierto con el mismo título se añade
un comentario en vez de crear otro.

### Autovalidación continua (selftest)

Mientras el 25-09 no esté liberado, cada corrida prueba además los selectores
contra **una fecha que sí está a la venta hoy** (ambas fuentes). Si en los logs ves
`selftest WARNING`, el HTML cambió y hay que ajustar selectores aunque el resultado
siga siendo `NOT_AVAILABLE`.

## Cambiar fecha / turnos / personas

Edita el bloque `CONFIG` al inicio de `scripts/check_borghese.py`:

```python
TARGET_DATE = "2026-09-25"                  # YYYY-MM-DD
TARGET_AFTERNOON_SLOTS = ["15:00", "17:00"]  # horas de entrada deseadas (24h)
PARTY_SIZE = 4
```

El título del Issue está en `.github/workflows/monitor.yml` (paso "Open alert issue").

## Ejecución

- Automática: cron `0 */3 * * *` (cada 3 h, UTC).
- Manual: pestaña *Actions* → *Borghese ticket monitor* → *Run workflow*
  (o `gh workflow run monitor.yml`).
- También corre en cada push que toque el script o el workflow, para validar
  selectores tras cada ajuste.

El script imprime `AVAILABLE` o `NOT_AVAILABLE` y siempre sale con código 0
(un fallo de scraping no debe parecer disponibilidad). El workflow compara la
línea exacta (`grep -qx`) porque `NOT_AVAILABLE` contiene `AVAILABLE`.

## Mantenimiento: si tosc.it (o GYG) cambian su HTML

Síntoma típico: `selftest WARNING` o errores de timeout de selectores en los logs.

1. Reproduce en local con navegador visible:
   `pip install playwright && playwright install chromium`, y en el script cambia
   temporalmente `headless=True` por `headless=False` (y `channel="chromium"` por
   `channel="chrome"` si tosc.it te bloquea).
2. Los selectores están **solo** en `scripts/check_borghese.py`:
   - tosc.it → funciones `tosc_api_released_days()` (API, raramente cambia) y
     `tosc_dom_slots()` (tarjetas `data-qa` de turnos).
   - GetYourGuide → `gyg_check_date()` y `gyg_collect_slot_chips()`.
   Cada función lleva comentado qué HTML espera encontrar.
3. Si la API de Eventim cambiara de forma, vuelca la respuesta con
   `curl '<URL de EVENTIM_API con product_group_id=2253937>' | python3 -m json.tool`
   y ajusta el parseo de `products[].typeAttributes.liveEntertainment.startDate`.
