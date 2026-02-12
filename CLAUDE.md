# CLAUDE.md - Agente 3: Registro Directo de Facturas CFDI

> Este archivo complementa el `CLAUDE.md` padre en `CarniceriaMa/`. El padre contiene el esquema de BD, patrones de conexion pyodbc y reglas generales de CFDI. Aqui solo va lo especifico del Agente 3.

---

## Que hace este proyecto

Automatiza el **registro manual** de facturas CFDI que no tienen remision asociada. El Agente 2 concilia facturas con remisiones Serie R; las que no matchean quedan como "Sin Remision" y las capturistas (MIREYA ALE, ABIGAIL RUIZ, etc.) las registran a mano. El Agente 3 reemplaza ese trabajo manual.

**Flujo:** XML CFDI -> parsear -> buscar proveedor por RFC -> matchear conceptos con catalogo ERP -> INSERT Serie F en SAVRecC/SAVRecD

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
│   │   ├── models.py               # ProductoERP, ProveedorERP, ResultadoRegistro, ResultadoMatchProducto
│   │   ├── proveedor_repo.py        # buscar_por_rfc() -> ProveedorERP
│   │   ├── producto_repo.py         # cargar_catalogo() -> List[ProductoERP]
│   │   └── registro_directo.py      # INSERT Serie F (cabecera + detalle)
│   ├── matching/
│   │   ├── cache_productos.py       # Indices en memoria: por_nombre, por_codigo_sat
│   │   ├── historial_compras.py     # Productos que el proveedor ha comprado antes + frecuencia
│   │   └── producto_matcher.py      # Pipeline de matching de 5 pasos (CORE)
│   └── reports/
│       └── excel_generator.py       # Reporte Excel con 6 hojas
```

**14 modulos totales.** 4 reutilizados de Agente2, 10 nuevos.

---

## Pipeline de Matching de Productos (CORE)

El motor de matching esta en `src/matching/producto_matcher.py`. Cascada de 5 pasos, del mas preciso al mas general:

```
Paso 1: MATCH EXACTO por nombre normalizado
  -> Busqueda O(1) en diccionario
  -> Si encuentra -> confianza 100%, nivel EXACTO

Paso 2: HISTORIAL PROVEEDOR + token_sort_ratio
  -> Productos que el proveedor ha comprado antes (query SAVRecD/SAVRecC)
  -> fuzz.token_sort_ratio >= 90 (UMBRAL_MATCH_PRODUCTO)
  -> nivel ALTA

Paso 2.5: HISTORIAL PROVEEDOR + token_set_ratio + frecuencia (NUEVO)
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
| `TimbradoFolioFiscal` | UUID CFDI | **UPPERCASE** |
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
| `Orden` | Secuencial desde 1 | |

### Registros parciales

Si `REGISTRAR_PARCIALES=true` (default) y >= 50% de conceptos matchean:
- Se crea el registro F con solo los conceptos matcheados
- SubTotal/Iva/Total se recalculan solo con los matcheados
- Los no matcheados se reportan en el Excel

### Validaciones de seguridad

1. **UUID unico**: `SELECT COUNT(*) FROM SAVRecC WHERE TimbradoFolioFiscal = ? AND Serie = 'F'`
2. **Proveedor existe**: Busqueda por RFC en SAVProveedor
3. **Transaccion atomica**: Todos los INSERTs en un solo `with cursor` (commit/rollback)

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

# BD
DB_SERVER=localhost              # localhost en el servidor, 100.73.181.41 via Tailscale
DB_DATABASE=DBSAV71_TEST         # DBSAV71 para produccion
DB_DRIVER={SQL Server Native Client 11.0}  # En el servidor Windows
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

## Reporte Excel (6 hojas)

1. **Resumen**: Totales procesados, exitosos, parciales, fallidos
2. **Exitosos**: Facturas F creadas completas
3. **Parciales**: Facturas F creadas con conceptos sin match
4. **No Registradas**: Facturas que no se pudieron registrar
5. **Sin Match**: Conceptos individuales que no matchearon
6. **Detalle Matching**: Log completo de cada concepto y su metodo de match

---

## Dependencias

```
python-dotenv>=1.0.0    # Cargar .env
pyodbc>=4.0.39          # SQL Server
lxml>=4.9.0             # Parser XML CFDI
openpyxl>=3.1.0         # Reportes Excel
rapidfuzz>=3.0.0        # Fuzzy matching (reemplazo de fuzzywuzzy)
loguru>=0.7.0           # Logging
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
F-67768 (AGRODAK, $1,900, 4 lineas), F-67769 (QUIRINO, $3,240, 1 linea parcial), F-67770 (AGROCHILEROS, $946.40, 2 lineas).
Creados con `Comprador='AGENTE3_SAT'`. Ver `RESULTADO_PRUEBAS.md` para detalles.

### Siguiente NumRec disponible
```sql
SELECT ISNULL(MAX(NumRec), 0) + 1 FROM SAVRecC WHERE Serie = 'F';
-- Al 12 Feb 2026: 67771 (post-pruebas)
```

---

## Convenciones

- **Idioma del codigo**: Espanol (variables, funciones, docstrings)
- **Compatibilidad Python**: 3.9+ (`typing.List`, `typing.Optional`, NO `list[X]` ni `X | None`)
- **Queries SQL**: Parametros `?` (pyodbc), nunca f-strings con datos de usuario
- **Transacciones**: Context manager con rollback automatico
- **Logging**: loguru, formato `{time} | {level} | {message}`
- **Desarrollo**: Mac (con MCP SQL Server via Tailscale)
- **Ejecucion**: Servidor Windows (localhost, SQL Server Native Client 11.0)
