# Informe Técnico para Desarrollador / IA: Error de Consola y Gestión de Imágenes de Dispositivos en Home Assistant

**Componente:** `custom_components/aigosmart`  
**Entorno:** Home Assistant 2026.9.0 (Frontend `frontend_latest`, Python 3.14)  
**Fecha:** 2026-09-03  

---

## 1. Error de Consola: `ha-config-device-page.ts:1201 Uncaught (in promise) {code: 'not_found', message: 'Domain not supported'}`

### Diagnóstico Exacto
Al abrir la página de configuración de cualquier dispositivo (`/config/devices/device/<device_id>`), el frontend de Home Assistant ejecuta el método `_getDiagnosticButtons()` en `ha-config-device-page.ts`.

Este método envía un comando WebSocket al backend:
```json
{
  "type": "diagnostics/get",
  "domain": "aigosmart"
}
```
El objetivo de esta consulta es averiguar si la integración dispone de manejadores de diagnóstico para mostrar el botón **"Descargar diagnóstico"** en la tarjeta de información del dispositivo.

Al no existir el módulo de plataforma `diagnostics.py` en `custom_components/aigosmart/`, el componente `diagnostics` del core de Home Assistant responde con el error WebSocket:
```json
{
  "code": "not_found",
  "message": "Domain not supported"
}
```

### Por qué aparece como "Uncaught (in promise)"
En el código fuente del frontend (`ha-config-device-page.ts`):
```typescript
try {
  t = await (0, S.g9)(this.hass, e.domain); // diagnostics/get
} catch (e) {
  if (e instanceof Error && e.message.includes("not_found")) return !1;
  throw e;
}
```
En Home Assistant, las respuestas de error de WebSocket se devuelven como objetos planos de JavaScript (`{code: "not_found", message: "Domain not supported"}`), **no** como instancias de `Error`. Por tanto:
1. `e instanceof Error` evalúa a `false`.
2. La excepción no se suprime y se ejecuta `throw e`.
3. Se produce el error no capturado en la consola del navegador.

### Solución Implementada
Se ha implementado el archivo `custom_components/aigosmart/diagnostics.py` con los métodos estándar de Home Assistant:
- `async_get_config_entry_diagnostics(hass, entry)`
- `async_get_device_diagnostics(hass, entry, device)`

Con este archivo presente, la consulta `diagnostics/get` devuelve éxito (`handlers: {config_entry: true, device: true}`), **el error de consola desaparece por completo** y los usuarios pueden descargar diagnósticos JSON nativos tanto de la integración como del dispositivo.

---

## 2. Por qué no aparece la imagen del dispositivo ("no aparece la imagen del dispositivo")

### Arquitectura de Home Assistant respecto a Imágenes de Hardware
Existe una confusión común sobre cómo Home Assistant gestiona las imágenes:

1. **`DeviceInfo` NO soporta `image_url`**:
   - En una versión anterior se añadió:
     ```python
     self._attr_device_info["image_url"] = image_url
     ```
   - En Home Assistant core, el diccionario `DeviceInfo` se desempaca directamente como argumentos de palabra clave (`**kwargs`) en `device_registry.async_get_or_create(...)`.
   - Dado que `image_url` no es un parámetro válido de `async_get_or_create`, Home Assistant arrojó un error fatal que impedía crear la entidad:
     ```text
     TypeError: async_get_or_create() got unexpected keyword arguments 'image_url'
     ```
   - **Por tanto, `image_url` nunca debe incluirse en `DeviceInfo`**.

2. **La carpeta `brand/devices/` no es un estándar de Home Assistant**:
   - El componente interno `brands` de Home Assistant (`homeassistant/components/brands/__init__.py`) solo busca archivos en la raíz de `brand/` que coincidan con la lista fija:
     `ALLOWED_IMAGES = {"icon.png", "logo.png", "icon@2x.png", "logo@2x.png", "dark_icon.png", "dark_logo.png"}`.
   - Cualquier subcarpeta como `brand/devices/` (con `category_light.png` o `panel_light_cct.png`) es **completamente ignorada** por el cargador de marcas de Home Assistant.

3. **Solo las integraciones de protocolos de hardware oficiales admiten fotos de dispositivos**:
   - En el frontend de Home Assistant (`ha-config-device-page.ts`), las fotos individuales de dispositivos (`/api/brands/hardware/<category>/<manufacturer_model>.png`) proceden del repositorio oficial CDN (`brands.home-assistant.io/hardware`) y están reservadas exclusivamente a protocolos físicos:
     - `zha` (Zigbee)
     - `zigbee2mqtt`
     - `zwave_js` (Z-Wave)
     - `matter`
     - `bluetooth`
     - `homeassistant_hardware`
   - Para el resto de integraciones (Tuya, Philips Hue, Shelly, Sonoff, AigoSmart, etc.), la interfaz de Home Assistant **no muestra fotos individuales de modelos en la página del dispositivo**. En su lugar, muestra:
     - El icono/logo de la marca de la integración (`aigosmart/logo.png` o `aigosmart/icon.png`).
     - El nombre de modelo (`model`) y fabricante (`manufacturer`) como texto en la tarjeta de información del dispositivo.

### Mejoras Realizadas para la Identidad Visual
Para que la integración se vea perfecta en la interfaz de Home Assistant:
- Se ha copiado `logo.png` y `logo@2x.png` a `custom_components/aigosmart/brand/`.
- Ahora `/api/brands/integration/aigosmart/logo.png` y `/api/brands/integration/aigosmart/icon.png` se sirven correctamente y se muestran en las tarjetas de la integración y del dispositivo.
