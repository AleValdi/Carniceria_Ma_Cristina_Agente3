# CLAUDE.md - Agente 3: Registro Directo de Facturas CFDI

> Este archivo complementa el `CLAUDE.md` padre en `CarniceriaMa/`. El padre contiene el esquema de BD, patrones de conexion pyodbc y reglas generales de CFDI. Aqui solo va lo especifico del Agente 3.

---

## Que hace este proyecto

Automatiza el **registro manual** de facturas CFDI que no tienen remision asociada. El Agente 2 concilia facturas con remisiones Serie R; las que no matchean quedan como "Sin Remision" y las capturistas (MIREYA ALE, ABIGAIL RUIZ, etc.) las registran a mano. El Agente 3 reemplaza ese trabajo manual.

**Flujo Ingreso:** XML CFDI -> parsear -> buscar proveedor por RFC -> matchear conceptos con catalogo ERP -> INSERT Serie F en SAVRecC/SAVRecD

**Flujo Egreso (NC):** XML CFDI Egreso -> parsear -> extraer UUID factura relacionada -> buscar factura F -> determinar tipo NC -> INSERT SAVNCredP/Det/Rec

---

## Diferencias con Agente 2

| Aspecto | Agente 2 (Conciliacion) | Agente 3 (Registro Directo) |
|---------|------------------------|---------------------------|
| Productos | Vienen de remisiones R | Se matchean del XML al catalogo ERP |
| Consolidacion | `Consolidacion = 1` | `Consolidacion = 0` |
| Remisiones | Vincula y actualiza Serie R | No hay remisiones involucradas |
| Comprador | De la remision original | `AGENTE3_SAT` |
| Dependencias | pandas, scipy, fuzzywuzzy | rapidfuzz, openpyxl (sin pandas) |

---

## Arquitectura y Modulos

```
Agente3/
├── main.py                          # CLI: --dry-run, --archivo, --test-conexion, --explorar-catalogo
├── config/
│   ├── settings.py                  # Settings dataclass con from_env()
│   └── database.py                  # Reutilizado de Agente2
├── src/
│   ├── sat/
│   │   ├── xml_parser.py            # Reutilizado de Agente2 (CFDIParser)
│   │   └── models.py               # Reutilizado de Agente2 (Factura, Concepto)
│   ├── erp/
│   │   ├── sav7_connector.py        # Reutilizado de Agente2
│   │   ├── utils.py                 # numero_a_letra() extraido de Agente2
│   │   ├── models.py               # ProductoERP, ProveedorERP, FacturaVinculada, RemisionPendiente, ResultadoRegistro
│   │   ├── proveedor_repo.py        # buscar_por_rfc() -> ProveedorERP
│   │   ├── producto_repo.py         # cargar_catalogo() -> List[ProductoERP]
│   │   ├── registro_directo.py      # INSERT Serie F (cabecera + detalle)
│   │   ├── registro_nc.py           # INSERT SAVNCredP/Det/Rec (Notas de Credito)
│   │   ├── factura_repo.py          # Buscar facturas F por UUID (para vincular NCs)
│   │   └── validacion_cruzada.py    # Validar remisiones pendientes antes de registrar
│   ├── cfdi/
│   │   ├── attachment_manager.py    # Copia XML a carpeta de red SAV7 + actualiza BD (DESACTIVADO temporalmente)
│   │   └── pdf_generator.py         # Genera PDF desde XML CFDI usando satcfdi (opcional)
│   ├── matching/
│   │   ├── cache_productos.py       # Indices en memoria + expandir_abreviaturas() + normalizar_texto()
│   │   ├── historial_compras.py     # Productos que el proveedor ha comprado antes + frecuencia
│   │   └── producto_matcher.py      # Pipeline de matching de 5 pasos + expansion abreviaturas (CORE)
│   └── reports/
│       └── excel_generator.py       # Reporte Excel con 7 hojas
```

**17 modulos totales.** 4 reutilizados de Agente2, 13 nuevos.

---

## Pipeline de Matching de Productos (CORE)

El motor de matching esta en `src/matching/producto_matcher.py`. Cascada de pasos, del mas preciso al mas general:

```
Paso 0: EXPANSION DE ABREVIATURAS (pre-procesamiento)
  -> Expande abreviaturas de marca: CHX->CHIMEX, SRF->SAN RAFAEL, MDLZ->MONDELEZ
  -> Normaliza unidades: GRS->G, KGS->KG, LTS->LT, MLS->ML, PZS/PZA->PZ
  -> Se aplica SOLO al texto XML, no al catalogo ERP
  -> Mejora scores: "SALCHICHA PAVO 500 GRS SRF" (75 pts) -> "SALCHICHA PAVO 500 G SAN RAFAEL" (98 pts)

Paso 1: MATCH EXACTO por nombre normalizado
  -> Busqueda O(1) en diccionario (primero original, luego expandido)
  -> Si encuentra -> confianza 100%, nivel EXACTO
  -> metodo_match = "exacto" o "exacto_expandido"

Paso 2: HISTORIAL PROVEEDOR + token_sort_ratio
  -> Productos que el proveedor ha comprado antes (query SAVRecD/SAVRecC)
  -> Usa nombre expandido (con abreviaturas resueltas)
  -> fuzz.token_sort_ratio >= 90 (UMBRAL_MATCH_PRODUCTO)
  -> nivel ALTA

Paso 2.5: HISTORIAL PROVEEDOR + token_set_ratio + frecuencia
  -> Mismos productos del historial, pero con token_set_ratio (mas permisivo)
  -> Maneja nombres abreviados: "JALAPENO" vs "CHILE JALAPENO VERDE" -> score 100
  -> Desambiguacion por frecuencia de compra: acepta si freq mejor >= 2x segundo
  -> Guards: min 5 chars, toggle desactivable, requiere freq > 0
  -> nivel ALTA, metodo_match = "historial_token_set"

Paso 3: CODIGO SAT + fuzzy
  -> Filtrar catalogo por ClaveProdServ del concepto XML
  -> fuzz.token_sort_ratio >= 90
  -> nivel ALTA

Paso 4: CATALOGO COMPLETO fuzzy (ultimo recurso)
  -> Contra los ~6,314 productos
  -> Threshold mas estricto: >= 95 (UMBRAL_MATCH_EXACTO)
  -> nivel MEDIA

Si nada matchea -> NO_MATCH, reportar para revision manual
```

### Paso 2.5: token_set_ratio + desambiguacion

`token_sort_ratio` penaliza nombres abreviados porque compara string completo. `token_set_ratio` compara interseccion de tokens:
- "JALAPENO" vs "CHILE JALAPENO VERDE" -> token_sort=57, **token_set=100**
- "ZANAHORIA 10KG" vs "ZANAHORIA" -> token_sort=73, **token_set=100**

Problema: genera ambiguedades (JALAPENO matchea con VERDE y ROJO al 100). Se resuelve con frecuencia historica:
- Si `freq_mejor >= 2 * freq_segundo` -> aceptar (senal fuerte)
- Si no -> rechazar como ambiguo (conservador)

**Guards de seguridad:**
1. Toggle global: `HABILITAR_TOKEN_SET_HISTORIAL=false` desactiva completamente
2. Longitud minima: `MIN_LONGITUD_TOKEN_SET=5` (evita "SAL", "RON" matcheando con todo)
3. Sin historial: no ejecutar sin datos del proveedor
4. Frecuencia cero: no aceptar sin evidencia historica
5. Empate sin ratio 2x: rechazar como ambiguo

**Campo `frecuencia` en ProductoERP:**
- `src/erp/models.py`: `frecuencia: int = 0` (default, backward compatible)
- Se popula en `historial_compras.py` via `obtener_productos_con_frecuencia()`
- Usa `dataclasses.replace()` para crear copias (no muta objetos del cache compartido)

### Normalizacion de texto

`normalizar_texto()` en `cache_productos.py`: uppercase, quitar acentos (unicodedata NFD), colapsar espacios, strip.

### Deteccion de ambiguedad

Pasos 2, 3 y 4: Si los 2 mejores candidatos puntuan con diferencia < 5 puntos (`margen_ambiguedad`), se rechaza el match y se reporta como ambiguo.

Paso 2.5: Ademas del margen de 5 puntos, usa frecuencia historica de compra para desambiguar empates.

### Libreria de fuzzy matching

Se usa **`rapidfuzz`** (NO fuzzywuzzy). API compatible pero compilada en C/C++, no necesita python-Levenshtein y tiene wheels precompilados para MSYS2.

```python
from rapidfuzz import fuzz
score = fuzz.token_sort_ratio(texto_a, texto_b)   # Pasos 2, 3, 4
score = fuzz.token_set_ratio(texto_a, texto_b)     # Paso 2.5
```

### Expansion de abreviaturas de marca (Paso 0)

Definido en `cache_productos.py`. Se ejecuta sobre el texto del XML antes de comparar con el catalogo.

**Abreviaturas conocidas** (extraidas de 275 registros de NCs SIGMA en produccion):

| Abreviatura | Marca completa | Fuente |
|-------------|---------------|--------|
| `CHX` | `CHIMEX` | XMLs NC SIGMA (embutidos) |
| `SRF` | `SAN RAFAEL` | XMLs NC SIGMA (salchichas) |
| `MDLZ` | `MONDELEZ` | XMLs NC SIGMA (Philadelphia) |
| `TGM` | `TANGAMANGA` | XMLs NC SIGMA |

**Normalizacion de unidades:**

| Variante | Canonica |
|----------|----------|
| `GRS`, `GR` | `G` |
| `KGS` | `KG` |
| `LTS` | `LT` |
| `MLS` | `ML` |
| `PZS`, `PZA` | `PZ` |

Soporta unidades pegadas a numeros: `800GRS` -> `800G`.

**Impacto medido:**
- `SALCHICHA PARA ASAR 800 GRS CHX` vs catalogo: 87 pts -> **95 pts** (+8)
- `SALCHICHA PAVO 500 GRS SRF` vs catalogo: 75 pts -> **98 pts** (+23)

Para agregar nuevas abreviaturas: editar `ABREVIATURAS_MARCA` en `cache_productos.py`.

---

## Validacion Cruzada de Remisiones (validacion_cruzada.py)

Antes de registrar una factura Serie F, el sistema verifica si el proveedor tiene remisiones pendientes (Serie R) en SAVRecC. Esto previene registros duplicados cuando el Agente 2 clasifica incorrectamente una factura como "Sin Remision".

### Clasificacion de seguridad

| Nivel | Condicion | Accion |
|-------|-----------|--------|
| **SEGURO** | Sin remisiones pendientes del proveedor | Registrar normalmente |
| **BLOQUEAR** | Proveedor tiene cualquier remision pendiente | NO registrar, reportar para revision manual |

### Query de remisiones pendientes

```sql
SELECT Serie, NumRec, Fecha, Total, Estatus, Factura, Proveedor
FROM SAVRecC
WHERE Proveedor = ?
  AND Serie = 'R'
  AND Estatus != 'Consolidada'
  AND Consolida = 0
ORDER BY Fecha DESC
```

Doble filtro (`Estatus != 'Consolidada'` + `Consolida = 0`) como cinturon de seguridad.

### Logica de clasificacion

1. Si `VALIDAR_REMISIONES_PENDIENTES=false` → SEGURO (toggle desactivado)
2. Si no hay remisiones pendientes → SEGURO
3. Si hay cualquier remision pendiente → **BLOQUEAR** (politica conservadora)

La comparacion de monto/fecha se usa solo para enriquecer el mensaje de bloqueo (identifica el mejor candidato), pero no cambia la clasificacion.

### Settings

| Variable | Default | Descripcion |
|----------|---------|-------------|
| `VALIDAR_REMISIONES_PENDIENTES` | `true` | Toggle para activar/desactivar |
| `TOLERANCIA_MONTO_VALIDACION` | `2.0` | % tolerancia para comparar montos |
| `DIAS_RANGO_VALIDACION` | `15` | +/- dias para comparar fechas |

### Error code

`REMISION_PENDIENTE` — La factura fue bloqueada porque el proveedor tiene remisiones pendientes. Aparece en la hoja "No Registradas" del reporte Excel.

### Punto de integracion

Se ejecuta en `main.py` entre la busqueda de proveedor y el matching de productos. Si va a BLOQUEAR, evita el costo del fuzzy matching.

---

## Adjuntos CFDI (attachment_manager.py) — DESACTIVADO

> **Estado actual:** Desactivado temporalmente por instruccion del cliente. Las llamadas a `AttachmentManager` estan comentadas en `registro_directo.py` y `registro_nc.py`. Como consecuencia, `TimbradoFolioFiscal` se guarda como `''` (vacio) en lugar del UUID, y los XML no se copian a la carpeta de red. La funcionalidad esta completa y lista para reactivar.

Despues de registrar una factura F o una NC NCF, el sistema copia el XML a la carpeta de red compartida de SAV7 y actualiza los campos de factura electronica en la BD. Replica el comportamiento del AttachmentManager del Agente 2.

### Carpeta de destino

`\\SERVERMC\Asesoft\SAV7-1\Recepciones CFDI` (configurable via `CFDI_ADJUNTOS_DIR`)

### Formatos de nombre

| Tipo | Formato | Ejemplo |
|------|---------|---------|
| Factura F | `{RFC}_REC_F{NumRec:06d}_{YYYYMMDD}` | `BBO911129DC4_REC_F068908_20260212` |
| NC NCF | `{RFC}_RECNC_NCF{NCredito:06d}_{YYYYMMDD}` | `SAC991222G1A_RECNC_NCF001197_20260211` |

### Campos actualizados en BD

**SAVRecC (Facturas F):**
- `FacturaElectronica` = nombre base (sin extension)
- `FacturaElectronicaExiste` = 1
- `FacturaElectronicaValida` = 1
- `FacturaElectronicaEstatus` = 'Vigente'

**SAVNCredP (NCs NCF):**
- `NCreditoElectronica` = nombre base (sin extension)
- `NCreditoElectronicaExiste` = 1
- `NCreditoElectronicaValida` = 1
- `NCreditoElectronicaEstatus` = 'Vigente'

### Patron no-bloqueante

Si la copia o actualizacion falla, se loguea warning pero **no se revierte el registro**. La factura F o NC se crea correctamente aunque el adjunto no se copie.

### Settings

| Variable | Default | Descripcion |
|----------|---------|-------------|
| `CFDI_ADJUNTOS_DIR` | `\\SERVERMC\Asesoft\SAV7-1\Recepciones CFDI` | Carpeta de red destino |
| `CFDI_ADJUNTOS_HABILITADO` | `true` | Toggle para activar/desactivar copia |

---

## Patron INSERT Serie F (registro_directo.py)

### Campos criticos SAVRecC (cabecera)

| Campo | Valor | Nota |
|-------|-------|------|
| `Serie` | `'F'` | Siempre F |
| `Comprador` | `'AGENTE3_SAT'` | Identifica registros del agente |
| `Capturo` | `'AGENTE3_SAT'` | Idem |
| `Estatus` | `'No Pagada'` | No usar "Pendiente" (solo tiene 3 registros en BD) |
| `Consolidacion` | `0` | **NO es consolidacion** |
| `Saldo` | = `Total` | No hay pagos aun |
| `Paridad` | `20` | Siempre para MXN |
| `Moneda` | `'PESOS'` | |
| `Sucursal` | `5` | Configurable en .env |
| `NumOC` | `0` | Sin orden de compra |
| `TimbradoFolioFiscal` | `''` (vacio) | UUID desactivado temporalmente (ver seccion Adjuntos CFDI). Al reactivar: UUID CFDI **UPPERCASE** |
| `Tipo` | `'Credito'` | Tipo de pago |
| `Referencia` | `'CREDITO'` | Referencia de pago |
| `SubTotal2` | = `SubTotal1` | Copia del subtotal |
| `TotalPrecio` | = `Total` | Copia del total |
| `TotalRecibidoNeto` | = `Total` | Copia del total |
| `SerieRFC` | `''` (vacio) | Campo requerido |
| `Articulos` | `round(suma_cantidades)` | Redondear: 199.8 -> 200 |
| `TotalLetra` | `numero_a_letra(total)` | Monto en palabras |
| `TipoRecepcion` | `'COMPRAS'` | |
| `Departamento` | `'NA'` | Consistente con registros manuales en produccion |
| `Afectacion` | `'TIENDA'` | |

### Campos criticos SAVRecD (detalle)

| Campo | Valor | Fuente |
|-------|-------|--------|
| `Producto` | Codigo ERP (ej: FYV002011) | Del catalogo, NO del XML |
| `Nombre` | Nombre ERP | Del catalogo, NO del XML |
| `Cantidad` | Del XML | `concepto.cantidad` |
| `Costo` | Del XML | `concepto.valor_unitario` |
| `CostoImp` | `0` | Patron de registros manuales |
| `PorcIva` | Del catalogo o XML | Preferir XML si difiere |
| `Unidad` | Del catalogo | Ej: KG, PZA, LT |
| `CodProv` | `''` (vacio) | Patron de registros manuales |
| `Talla` | = `Orden` | Numero secuencial (mismo valor que Orden) |
| `Orden` | Secuencial desde 1 | |

### Registros parciales

Si `REGISTRAR_PARCIALES=true` (default) y >= 50% de conceptos matchean:
- Se crea el registro F con solo los conceptos matcheados
- SubTotal/Iva/Total se recalculan solo con los matcheados
- Los no matcheados se reportan en el Excel

### Validaciones de seguridad

1. **UUID unico**: `SELECT COUNT(*) FROM SAVRecC WHERE TimbradoFolioFiscal = ? AND Serie = 'F'`
2. **Factura duplicada**: `SELECT COUNT(*) FROM SAVRecC WHERE Serie=? AND RFC=? AND Factura=? AND Total=? AND Fecha=?` (para UUID vacio)
3. **Proveedor existe**: Busqueda por RFC en SAVProveedor
4. **Transaccion atomica**: Todos los INSERTs en un solo `with cursor` (commit/rollback)

### Campos INSERT totales

- **SAVRecC (cabecera)**: 46 campos (ver query completo en `registro_directo.py`)
- **SAVRecD (detalle)**: 29 campos por linea

---

## Notas de Credito (registro_nc.py)

### Tablas del Ecosistema NC

Las notas de credito de proveedores en SAV7 **NO van en SAVRecC**. Tienen tablas separadas:

| Tabla | Funcion | Equivalente |
|-------|---------|-------------|
| **SAVNCredP** | Encabezado NC proveedor | equiv. a SAVRecC |
| **SAVNCredPDet** | Detalle/productos de la NC | equiv. a SAVRecD |
| **SAVNCredPRec** | Vinculacion NC <-> Factura F | tabla puente |
| **SAVNCredPTipo** | Catalogo de tipos de NC (4 tipos) | catalogo |

### Tipos de NC

| Tipo | Descripcion | Producto |
|------|-------------|----------|
| **DEVOLUCIONES** | Producto devuelto fisicamente | Productos reales matcheados |
| **DESCUENTOS** | Descuento comercial / bonificacion | INSADM094 (generico, Servicio=1) |
| **AJUSTES** | Ajustes varios | (no implementado aun) |
| **GARANTIAS** | Producto en garantia | (no implementado aun) |

### Determinacion del tipo NC

```
1. Si RFC emisor NO esta en RFC_PROVEEDORES_CON_DESCUENTO -> DEVOLUCIONES (siempre)
2. Si RFC es SIGMA (SAC991222G1A) y claves SAT son genericas (01010101, 60010100, 84111506) -> DESCUENTOS
3. Si RFC es SIGMA pero conceptos tienen productos reales -> DEVOLUCIONES
```

**Regla de negocio critica:** Solo SIGMA (RFC `SAC991222G1A`) puede tener DESCUENTOS.
Todos los demas proveedores siempre son DEVOLUCIONES, independientemente de la ClaveProdServ.
Esto se controla con la constante `RFC_PROVEEDORES_CON_DESCUENTO` en `registro_nc.py`.

### Campos criticos SAVNCredP (encabezado)

| Campo | Valor | Nota |
|-------|-------|------|
| `Serie` | `'NCF'` | Serie dominante en produccion |
| `NCredito` | Secuencial | `MAX(NCredito)+1 WHERE Serie='NCF'` |
| `Proveedor` | Clave ERP | De proveedor_repo |
| `Concepto` | `"DEVOLUCION RECOC F-{NumRec} FACT: {Factura} FECHA: {dd/MMM/yyyy}"` | Truncar a 60 chars |
| `Estatus` | `'No Aplicada'` | **Sin acreditacion automatica** |
| `TipoNCredito` | `'DEVOLUCIONES'` o `'DESCUENTOS'` | Segun logica de determinacion |
| `TimbradoFolioFiscal` | `''` (vacio) | UUID desactivado temporalmente (ver seccion Adjuntos CFDI). Al reactivar: UUID CFDI Egreso **UPPERCASE** |
| `NCreditoProv` | `factura_sat.folio` | Numero de NC del proveedor |
| `Comprador` | `'AGENTE3_SAT'` | Identifica registros del agente |
| `TotalAcredita` | Total NC | Para tracking |

### Campos criticos SAVNCredPDet (detalle)

**DESCUENTOS:** Una sola linea con producto generico INSADM094 (cantidad=1, costo=subtotal, Servicio=1)
**DEVOLUCIONES:** Una linea por producto matcheado (igual que SAVRecD en registro_directo.py)

### Campos criticos SAVNCredPRec (vinculacion)

| Campo | Valor |
|-------|-------|
| `RecSerie` | `'F'` |
| `RecNumRec` | NumRec de la factura vinculada |
| `RecUUID` | UUID de la factura vinculada |
| `RecAcredita` | `0` (no acreditado aun, se llenara al acreditar manualmente) |
| `RecAcredito` | `NULL` (no se acredita automaticamente) |
| `RecAcreditoCapturo` | `''` (vacio, se llenara al acreditar) |

### Flujo de NC en el sistema

```
1. Proveedor emite CFDI tipo Egreso con UUID propio
2. XML tiene nodo cfdi:CfdiRelacionados con UUID de factura original
3. Agente 3 detecta TipoDeComprobante="E" -> flujo NC
4. Busca factura F en SAVRecC por UUID relacionado
5. Valida: factura existe, proveedor coincide, estatus='No Pagada'
6. Determina tipo NC (DEVOLUCIONES o DESCUENTOS)
7. Transaccion atomica:
   a. INSERT SAVNCredP (encabezado con Estatus='No Aplicada')
   b. INSERT SAVNCredPDet (detalle productos o generico)
   c. INSERT SAVNCredPRec (vinculacion con factura F)
   -- NO actualiza NCredito/Saldo en SAVRecC (decision del cliente)
```

### Relacion con SAVRecC

La formula en SAVRecC es: `Saldo = Total - NCredito - Pagado`

El campo `NCredito` de SAVRecC se actualiza **solo cuando se acredita manualmente** la NC (cambia Estatus de 'No Aplicada' a 'Acreditada'). El Agente 3 NO hace esta acreditacion.

### Verificacion en BD

```sql
-- NCs creadas por Agente 3
SELECT Serie, NCredito, Proveedor, ProveedorNombre, TipoNCredito,
       Total, Estatus, TimbradoFolioFiscal, Comprador
FROM SAVNCredP WHERE Comprador = 'AGENTE3_SAT';

-- Vinculacion con facturas
SELECT p.Serie, p.NCredito, p.TipoNCredito, p.Total as TotalNC,
       r.RecSerie, r.RecNumRec, r.RecUUID, r.RecAcredita
FROM SAVNCredP p
INNER JOIN SAVNCredPRec r ON p.Serie = r.Serie AND p.NCredito = r.NCredito
WHERE p.Comprador = 'AGENTE3_SAT';
```

### Reversion de prueba NC

```sql
-- 1. Borrar vinculacion
DELETE FROM SAVNCredPRec WHERE Serie='NCF' AND NCredito = @nc_num;
-- 2. Borrar detalle
DELETE FROM SAVNCredPDet WHERE Serie='NCF' AND NCredito = @nc_num;
-- 3. Borrar encabezado
DELETE FROM SAVNCredP WHERE Serie='NCF' AND NCredito = @nc_num;
-- No hace falta restaurar SAVRecC porque no se modifica NCredito/Saldo
```

---

## Configuracion (.env y .env.local)

El sistema carga variables de entorno en dos capas:
1. `.env` — configuracion base (servidor de produccion/test)
2. `.env.local` — override local para desarrollo remoto (si existe)

`config/settings.py` carga primero `.env` y luego `.env.local` con `override=True`. Esto permite que cada desarrollador tenga su propia configuracion de conexion sin modificar el `.env` del servidor.

### .env (servidor, NO modificar para desarrollo)

```env
# Matching
UMBRAL_MATCH_PRODUCTO=90        # Minimo fuzzy score (pasos 2, 2.5, 3)
UMBRAL_MATCH_EXACTO=95          # Minimo fuzzy score (paso 4, catalogo completo)
MIN_CONCEPTOS_MATCH_PORCENTAJE=50  # % minimo para registrar
REGISTRAR_PARCIALES=true        # Registrar con match parcial

# Matching avanzado (paso 2.5)
HABILITAR_TOKEN_SET_HISTORIAL=true   # Toggle para paso 2.5 (false = pipeline original de 4 pasos)
MIN_LONGITUD_TOKEN_SET=5             # Min chars para activar token_set_ratio

# Registro
ESTATUS_REGISTRO=No Pagada
USUARIO_SISTEMA=AGENTE3_SAT
SUCURSAL=5
NUMREC_RANGO_MINIMO=900000      # Rango reservado para evitar colision con ERP (~68,000)
NCREDITO_RANGO_MINIMO=50000     # Rango reservado para NCs (ERP usa ~1,200)
PRODUCTO_DESCUENTO=INSADM094    # Producto generico para NCs tipo DESCUENTOS

# BD
DB_SERVER=localhost              # localhost en el servidor, 100.73.181.41 via Tailscale
DB_DATABASE=DBSAV71A             # Sandbox. DBSAV71 para produccion
DB_DRIVER={SQL Server Native Client 11.0}  # En el servidor Windows
DB_TRUSTED_CONNECTION=false      # true = Windows Auth (sin usuario/password)
DB_TIMEOUT=30                    # Timeout de conexion en segundos
```

### .env.local (desarrollo remoto, NO se commitea)

Solo define las variables que cambian respecto al `.env` base:

```env
DB_SERVER=100.73.181.41
DB_DRIVER={ODBC Driver 18 for SQL Server}
DB_USERNAME=tu_usuario
DB_PASSWORD=tu_password
```

Para configurar: `cp .env.local.example .env.local` y llenar credenciales. El `.gitignore` ya excluye `.env.local`.

---

## CLI (main.py)

```bash
python main.py                        # Procesar todos los XMLs en data/xml_entrada/
python main.py --dry-run              # Simular sin escribir en BD
python main.py --archivo factura.xml  # Procesar un solo XML
python main.py --test-conexion        # Probar conexion BD y mostrar estadisticas
python main.py --explorar-catalogo    # Cargar catalogo y mostrar estadisticas
```

---

## Reporte Excel (7 hojas)

1. **Resumen**: Totales procesados, exitosos, parciales, fallidos (Ingreso + NC separados); desglose por tipo de error; cuenta de bloqueadas por remision
2. **Exitosos**: Facturas F Ingreso creadas completas (no parciales, no NC)
3. **Parciales**: Facturas F Ingreso creadas con conceptos sin match
4. **NCs Registradas**: Notas de Credito Egreso registradas exitosamente
5. **No Registradas**: Facturas y NCs que no se pudieron registrar (ambos tipos combinados)
6. **Sin Match**: Conceptos individuales que no matchearon con ningun producto ERP
7. **Detalle Matching**: Log completo de cada concepto y su metodo de match, score, nivel, candidatos descartados

---

## Dependencias

```
python-dotenv>=1.0.0    # Cargar .env
pyodbc>=4.0.39          # SQL Server
lxml>=4.9.0             # Parser XML CFDI
openpyxl>=3.1.0         # Reportes Excel
rapidfuzz>=3.0.0        # Fuzzy matching (reemplazo de fuzzywuzzy)
loguru>=0.7.0           # Logging
satcfdi>=4.4.0          # Generacion de PDF desde XML CFDI (opcional, import graceful)
pytest>=7.4.0           # Testing
```

**NO se usa:** pandas, numpy, scipy, python-Levenshtein, fuzzywuzzy. Se eliminaron por problemas de compilacion en MSYS2 y porque no se importan en ningun modulo.

---

## Entorno del Servidor (Windows + MSYS2)

El servidor usa Python de MSYS2 (`C:\msys64\mingw64\`), lo cual tiene particularidades:

### Activar venv
```powershell
.\venv\bin\Activate.ps1    # NO .\venv\Scripts\Activate.ps1
```
MSYS2 Python crea venvs con estructura Unix (`bin/` en vez de `Scripts/`).

### Instalar dependencias
```powershell
# Paquetes C (no compilan con pip en MSYS2):
# Instalar via pacman en terminal MSYS2:
pacman -S mingw-w64-x86_64-python-pyodbc
pacman -S mingw-w64-x86_64-python-lxml

# Recrear venv con acceso a paquetes del sistema:
python -m venv --system-site-packages venv

# Paquetes puros Python (con workaround SSL):
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt
```

### Problemas conocidos
- **SSL roto en pip**: Usar `--trusted-host` siempre
- **cmake/ninja no disponibles**: No se pueden compilar extensiones C via pip, usar pacman
- **pacman copy-paste**: No pegar comandos en MSYS2 terminal, escribirlos manualmente (bracketed paste interfiere)

---

## Verificacion en BD

### Identificar registros del Agente 3
```sql
SELECT * FROM SAVRecC WHERE Comprador = 'AGENTE3_SAT' AND Serie = 'F';
```

### Comparar con registro manual de referencia
NumRec **67742** (ANTONIO EDUARDO QUIRINO): 2 lineas de HUEVO, Articulos=200, Total=$9,850
NumRec **67752** (PRODUCTOS TRESAN): Con UUID, Articulos=610, Total=$72,606.40

### Revertir datos de prueba
```sql
-- Eliminar detalle PRIMERO
DELETE FROM SAVRecD WHERE Serie = 'F' AND NumRec = @numrec;
-- Luego encabezado
DELETE FROM SAVRecC WHERE Serie = 'F' AND NumRec = @numrec;
```

### Registros de prueba existentes en TEST

**Facturas Ingreso (Serie F):**
F-67768 (AGRODAK, $1,900, 4 lineas), F-67769 (QUIRINO, $3,240, 1 linea parcial), F-67770 (AGROCHILEROS, $946.40, 2 lineas).
Creados con `Comprador='AGENTE3_SAT'`. Ver `RESULTADO_PRUEBAS.md` para detalles.

**Notas de Credito (Serie NCF):**
NCF-1176 (SIGMA, DEVOLUCIONES, $286.30, 2 lineas — matcheo 2/4 conceptos), NCF-1177 (SIGMA, DESCUENTOS, $836.00, INSADM094).
Ambas vinculadas a F-68721 (copiada de PROD para test). Comprador='AGENTE3_SAT'.
Equivalentes en produccion: NCF-1195 (DESCUENTOS) y NCF-1196 (DEVOLUCIONES).

### Siguiente NumRec disponible
```sql
SELECT ISNULL(MAX(NumRec), 0) + 1 FROM SAVRecC WHERE Serie = 'F';
-- Al 12 Feb 2026: 67771 (post-pruebas)
```

---

## Codigos de Error (ResultadoRegistro.error)

| Codigo | Contexto | Descripcion |
|--------|----------|-------------|
| `TIPO_NO_SOPORTADO` | Ingreso/Egreso | Tipo comprobante T, N o P (no soportado) |
| `SIN_CONCEPTOS` | Ingreso | XML sin conceptos parseable |
| `PROVEEDOR_NO_ENCONTRADO` | Ingreso/Egreso | RFC no existe en SAVProveedor |
| `REMISION_PENDIENTE` | Ingreso | Validacion cruzada detecto remision similar (BLOQUEAR) |
| `INSUFICIENTES_MATCHES` | Ingreso | Menos del 50% de conceptos matchearon |
| `UUID_DUPLICADO` | Ingreso | UUID ya existe en SAVRecC Serie F |
| `FACTURA_DUPLICADA` | Ingreso | Combinacion RFC+Factura+Total+Fecha ya existe (para UUID vacio) |
| `SIN_CFDI_RELACIONADO` | Egreso (NC) | XML Egreso sin nodo CfdiRelacionados |
| `FACTURA_VINCULADA_NO_ENCONTRADA` | Egreso (NC) | UUID relacionado no encontrado en SAVRecC Serie F |
| `FACTURA_NO_APTA` | Egreso (NC) | Factura vinculada no cumple validacion (estatus, proveedor, saldo) |
| `UUID_NC_DUPLICADO` | Egreso (NC) | UUID ya existe en SAVNCredP Serie NCF |
| `NC_DUPLICADA` | Egreso (NC) | Combinacion RFC+NCreditoProv+Total ya existe |

---

## Convenciones

- **Idioma del codigo**: Espanol (variables, funciones, docstrings)
- **Compatibilidad Python**: 3.9+ (`typing.List`, `typing.Optional`, NO `list[X]` ni `X | None`)
- **Queries SQL**: Parametros `?` (pyodbc), nunca f-strings con datos de usuario
- **Transacciones**: Context manager con rollback automatico
- **Logging**: loguru, formato `{time} | {level} | {message}`
- **Desarrollo**: Mac (con MCP SQL Server via Tailscale)
- **Ejecucion**: Servidor Windows (localhost, SQL Server Native Client 11.0)
