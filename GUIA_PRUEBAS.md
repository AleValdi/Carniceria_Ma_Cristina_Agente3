# Guia de Pruebas - Agente 3: Registro Directo CFDI

Documento de referencia para ejecutar pruebas del Agente 3 en el servidor contra `DBSAV71_TEST`.

---

## Tabla de Contenido

1. [Requisitos Previos](#1-requisitos-previos)
2. [Configuracion del Entorno](#2-configuracion-del-entorno)
3. [Pruebas Basicas (Sin Escritura en BD)](#3-pruebas-basicas-sin-escritura-en-bd)
4. [Pruebas con Dry-Run](#4-pruebas-con-dry-run)
5. [Prueba de Registro Real en TEST](#5-prueba-de-registro-real-en-test)
6. [Verificacion en Base de Datos](#6-verificacion-en-base-de-datos)
7. [Reversion de Datos de Prueba](#7-reversion-de-datos-de-prueba)
8. [Errores Comunes y Soluciones](#8-errores-comunes-y-soluciones)
9. [Referencia: Datos Existentes en BD](#9-referencia-datos-existentes-en-bd)

---

## 1. Requisitos Previos

### En el servidor Windows (donde se ejecuta el agente)

- Python 3.9+ (MSYS2: `C:\msys64\mingw64\bin\python3.exe`)
- SQL Server con `DBSAV71_TEST` accesible en `localhost`
- Driver: `SQL Server Native Client 11.0`
- Paquetes del sistema (via pacman): `pyodbc`, `lxml`

### En la maquina de desarrollo (Mac)

- MCP SQL Server conectado a `100.73.181.41` (Tailscale) para verificacion remota
- Acceso al repositorio Git

### Archivos XML de prueba

Necesitas al menos 1 XML CFDI de tipo Ingreso en `data/xml_entrada/`. Debe ser:
- Un XML valido CFDI 4.0 o 3.3
- `TipoDeComprobante="I"` (Ingreso)
- RFC del emisor que exista en `SAVProveedor`
- UUID que **NO** exista ya en `SAVRecC.TimbradoFolioFiscal`

---

## 2. Configuracion del Entorno

### 2.1 Activar el entorno virtual

```powershell
cd C:\ruta\al\Agente3
.\venv\bin\Activate.ps1
```

> **IMPORTANTE**: El Python de MSYS2 crea venvs con estructura Unix (`bin/` en vez de `Scripts/`).
> Si ves error "Activate.ps1 not found" en `Scripts\`, usa `bin\` en su lugar.

### 2.2 Verificar .env

Asegurate de que `.env` contiene:

```env
DB_SERVER=localhost
DB_PORT=1433
DB_DATABASE=DBSAV71_TEST
DB_USERNAME=devsav7
DB_PASSWORD=devsav7
DB_DRIVER={SQL Server Native Client 11.0}
DB_TRUSTED_CONNECTION=false
DB_TIMEOUT=30

UMBRAL_MATCH_PRODUCTO=90
UMBRAL_MATCH_EXACTO=95
MIN_CONCEPTOS_MATCH_PORCENTAJE=50
REGISTRAR_PARCIALES=true

ESTATUS_REGISTRO=No Pagada
USUARIO_SISTEMA=AGENTE3_SAT
SUCURSAL=5

LOG_LEVEL=INFO
```

### 2.3 Verificar directorios

```powershell
# Deben existir estos directorios
ls data\xml_entrada\
ls data\xml_procesados\
ls data\xml_parciales\
ls data\xml_fallidos\
ls data\reportes\
ls logs\
```

Si no existen, crearlos:

```powershell
mkdir data\xml_entrada, data\xml_procesados, data\xml_parciales, data\xml_fallidos, data\reportes, logs
```

---

## 3. Pruebas Basicas (Sin Escritura en BD)

### 3.1 Test de conexion

```powershell
python main.py --test-conexion
```

**Resultado esperado:**
```
Conexion exitosa a DBSAV71_TEST en localhost
Recepciones en BD: ~111,181
Productos en catalogo: ~6,314
```

**Si falla, ver:** [Error de conexion a BD](#81-error-de-conexion-a-bd)

### 3.2 Explorar catalogo

```powershell
python main.py --explorar-catalogo
```

**Resultado esperado:**
```
Catalogo de productos cargado: ~6,314 productos
Nombres unicos: ~6,217
Codigos SAT distintos: ~607
Top 10 codigos SAT mas frecuentes: ...
Ejemplos de productos: ...
```

Este comando NO escribe en la BD. Solo carga el catalogo en memoria y muestra estadisticas.

**Si falla, ver:** [Error al cargar catalogo](#82-error-al-cargar-catalogo)

---

## 4. Pruebas con Dry-Run

El modo `--dry-run` ejecuta todo el pipeline EXCEPTO los INSERTs en la BD. Ideal para validar matching sin riesgo.

### 4.1 Procesar un solo XML

```powershell
python main.py --dry-run --archivo data\xml_entrada\factura_prueba.xml
```

**Resultado esperado:**
```
[DRY-RUN] Simulacion sin escritura en BD
Procesando: factura_prueba.xml
  Proveedor encontrado: XXXXX (clave: 001XXX)
  Matching conceptos: X/Y matcheados
  [DRY-RUN] Se habria creado Serie F NumRec XXXXX
```

**Que verificar:**
- Que el proveedor se encuentra por RFC
- Que los conceptos matchean con productos del catalogo
- Que el metodo de match tiene sentido (exacto, historial, codigo_sat, fuzzy_global)
- Que los scores de confianza son razonables (>90%)

### 4.2 Procesar todos los XMLs

```powershell
python main.py --dry-run
```

Procesa todos los XMLs en `data/xml_entrada/`. Genera reporte Excel en `data/reportes/`.

**Si falla, ver:** [Error de parsing XML](#83-error-de-parsing-xml) o [Error de matching](#84-error-de-matching)

---

## 5. Prueba de Registro Real en TEST

> **PRECAUCION**: Este paso ESCRIBE en `DBSAV71_TEST`. Asegurate de estar en la base de TEST, no produccion.

### 5.1 Verificar que estamos en TEST

```powershell
# En .env debe decir:
# DB_DATABASE=DBSAV71_TEST
```

### 5.2 Anotar el siguiente NumRec antes de ejecutar

Consultar via MCP o SQL directo:

```sql
SELECT ISNULL(MAX(NumRec), 0) + 1 AS SiguienteNumRec FROM SAVRecC WHERE Serie = 'F';
```

> **Valor actual (Feb 2026):** El siguiente NumRec disponible es **67768**.
> Anotar este numero para poder verificar y/o revertir despues.

### 5.3 Ejecutar con un solo XML

```powershell
python main.py --archivo data\xml_entrada\factura_prueba.xml
```

**Resultado esperado:**
```
Procesando: factura_prueba.xml
  Proveedor encontrado: XXXXX
  Matching conceptos: X/Y matcheados
  Registro creado: Serie F NumRec 67768
  XML movido a: data\xml_procesados\
Reporte generado: data\reportes\agente3_YYYYMMDD_HHMMSS.xlsx
```

### 5.4 Verificar inmediatamente en BD

Ver seccion [6. Verificacion en Base de Datos](#6-verificacion-en-base-de-datos).

---

## 6. Verificacion en Base de Datos

Estas consultas se ejecutan via MCP SQL Server desde la maquina de desarrollo, o directamente en SQL Server Management Studio en el servidor.

### 6.1 Buscar registros creados por Agente3

```sql
-- Todos los registros creados por AGENTE3_SAT
SELECT Serie, NumRec, Proveedor, ProveedorNombre, Fecha,
       Comprador, Estatus, SubTotal1, Iva, Total, Saldo,
       TimbradoFolioFiscal, Consolidacion, Articulos, Partidas,
       Paridad, Moneda, MetododePago, Sucursal
FROM SAVRecC
WHERE Comprador = 'AGENTE3_SAT' AND Serie = 'F'
ORDER BY NumRec DESC;
```

### 6.2 Verificar encabezado (SAVRecC)

Comparar el registro creado con uno manual existente:

```sql
-- Registro del Agente3 vs registro manual de referencia
SELECT
    'AGENTE3' as Origen, Serie, NumRec, Proveedor, ProveedorNombre,
    Estatus, SubTotal1, Iva, Total, Saldo, Consolidacion,
    Articulos, Partidas, Paridad, Moneda, Sucursal, NumOC,
    MetododePago, TimbradoFolioFiscal, TotalLetra
FROM SAVRecC
WHERE Serie = 'F' AND NumRec = @numrec_agente3

UNION ALL

SELECT
    'MANUAL' as Origen, Serie, NumRec, Proveedor, ProveedorNombre,
    Estatus, SubTotal1, Iva, Total, Saldo, Consolidacion,
    Articulos, Partidas, Paridad, Moneda, Sucursal, NumOC,
    MetododePago, TimbradoFolioFiscal, TotalLetra
FROM SAVRecC
WHERE Serie = 'F' AND NumRec = 67752;  -- Registro manual con UUID
```

**Campos criticos a verificar:**

| Campo | Valor Esperado | Notas |
|-------|---------------|-------|
| `Serie` | `F` | Siempre F |
| `Comprador` | `AGENTE3_SAT` | Identifica registros del agente |
| `Estatus` | `No Pagada` | Estado inicial |
| `Consolidacion` | `0` (false) | NO es consolidacion |
| `Saldo` | Igual a `Total` | No hay pagos aun |
| `Paridad` | `20` | Siempre 20 para MXN |
| `Moneda` | `PESOS` | Siempre PESOS |
| `Sucursal` | `5` | Sucursal por defecto |
| `NumOC` | `0` | Sin orden de compra |
| `TimbradoFolioFiscal` | UUID del CFDI | En UPPERCASE |
| `Articulos` | `round(suma_cantidades)` | Redondeado |
| `Partidas` | Numero de lineas detalle | Solo conceptos matcheados |
| `TotalLetra` | Monto en palabras | Ej: "NUEVE MIL OCHOCIENTOS..." |

### 6.3 Verificar detalle (SAVRecD)

```sql
-- Lineas de detalle del registro del Agente3
SELECT Serie, NumRec, Orden, Producto, Nombre,
       Cantidad, Costo, CostoImp, PorcIva, Unidad, CodProv
FROM SAVRecD
WHERE Serie = 'F' AND NumRec = @numrec_agente3
ORDER BY Orden;
```

**Comparar con registro manual de referencia:**

```sql
-- Detalle de registro manual 67742 (ANTONIO EDUARDO QUIRINO - HUEVO)
SELECT Serie, NumRec, Orden, Producto, Nombre,
       Cantidad, Costo, CostoImp, PorcIva, Unidad, CodProv
FROM SAVRecD
WHERE Serie = 'F' AND NumRec = 67742
ORDER BY Orden;
```

**Referencia del registro manual 67742:**

| Orden | Producto | Nombre | Cantidad | Costo | CostoImp | PorcIva | Unidad |
|-------|----------|--------|----------|-------|----------|---------|--------|
| 1 | ABA002009 | HUEVO SAN JUAN 12 PZAS | 150 | 36 | 0 | 0 | PZA |
| 2 | ABA002008 | HUEVO SAN JUAN 30 PZAS | 50 | 89 | 0 | 0 | PZA |

**Campos criticos del detalle:**

| Campo | Valor Esperado | Notas |
|-------|---------------|-------|
| `Producto` | Codigo del catalogo ERP | Ej: ABA002009 |
| `Nombre` | Nombre del catalogo ERP | NO del XML |
| `Cantidad` | Del XML | `concepto.cantidad` |
| `Costo` | Del XML | `concepto.valor_unitario` |
| `CostoImp` | `0` | Patron de registros manuales |
| `PorcIva` | Del catalogo ERP | O del XML si difiere |
| `Unidad` | Del catalogo ERP | Ej: KG, PZA, LT |
| `CodProv` | `''` o `NULL` | Vacio en registros manuales |
| `Orden` | Secuencial desde 1 | 1, 2, 3... |

### 6.4 Verificar que no hay duplicados

```sql
-- Verificar que el UUID no se registro dos veces
SELECT Serie, NumRec, TimbradoFolioFiscal, Comprador
FROM SAVRecC
WHERE TimbradoFolioFiscal = '@uuid_del_xml'
AND Serie = 'F';
```

Debe retornar **exactamente 1 fila**. Si retorna 2+, hay un problema de duplicacion.

### 6.5 Verificar consistencia montos

```sql
-- Los montos del encabezado deben cuadrar con la suma del detalle
SELECT
    c.NumRec,
    c.SubTotal1 AS SubTotal_Encabezado,
    c.Iva AS Iva_Encabezado,
    c.Total AS Total_Encabezado,
    SUM(d.Cantidad * d.Costo) AS SubTotal_Calculado,
    c.Partidas AS Partidas_Encabezado,
    COUNT(*) AS Partidas_Reales,
    c.Articulos AS Articulos_Encabezado,
    ROUND(SUM(d.Cantidad), 0) AS Articulos_Calculado
FROM SAVRecC c
INNER JOIN SAVRecD d ON c.Serie = d.Serie AND c.NumRec = d.NumRec
WHERE c.Comprador = 'AGENTE3_SAT' AND c.Serie = 'F'
GROUP BY c.NumRec, c.SubTotal1, c.Iva, c.Total, c.Partidas, c.Articulos;
```

**Verificar:**
- `SubTotal_Encabezado` debe ser cercano a `SubTotal_Calculado` (puede diferir en registros parciales)
- `Partidas_Encabezado` = `Partidas_Reales`
- `Articulos_Encabezado` = `Articulos_Calculado`

> **Nota sobre registros parciales**: Si `REGISTRAR_PARCIALES=true` y no todos los conceptos matchearon, el SubTotal del encabezado corresponde al XML completo, pero el detalle solo tiene los conceptos matcheados. Esto es intencional y replica el comportamiento manual donde las capturistas a veces registran solo lo que encuentran en el catalogo.

---

## 7. Reversion de Datos de Prueba

### 7.1 Eliminar un registro especifico

```sql
-- PASO 1: Identificar el registro a eliminar
SELECT Serie, NumRec, Proveedor, ProveedorNombre, Total, Comprador
FROM SAVRecC
WHERE Comprador = 'AGENTE3_SAT' AND Serie = 'F';

-- PASO 2: Eliminar detalle PRIMERO (por integridad referencial)
DELETE FROM SAVRecD WHERE Serie = 'F' AND NumRec = @numrec;

-- PASO 3: Eliminar encabezado
DELETE FROM SAVRecC WHERE Serie = 'F' AND NumRec = @numrec;

-- PASO 4: Verificar eliminacion
SELECT * FROM SAVRecC WHERE Serie = 'F' AND NumRec = @numrec;
-- Debe retornar 0 filas
```

### 7.2 Eliminar TODOS los registros del Agente3

```sql
-- PRECAUCION: Esto elimina TODOS los registros creados por AGENTE3_SAT
-- Solo ejecutar en DBSAV71_TEST

-- Primero ver que se va a eliminar
SELECT Serie, NumRec, ProveedorNombre, Total
FROM SAVRecC WHERE Comprador = 'AGENTE3_SAT' AND Serie = 'F';

-- Eliminar detalles
DELETE d FROM SAVRecD d
INNER JOIN SAVRecC c ON d.Serie = c.Serie AND d.NumRec = c.NumRec
WHERE c.Comprador = 'AGENTE3_SAT' AND c.Serie = 'F';

-- Eliminar encabezados
DELETE FROM SAVRecC WHERE Comprador = 'AGENTE3_SAT' AND Serie = 'F';
```

### 7.3 Mover XMLs de vuelta para re-procesar

```powershell
# Mover XMLs procesados de vuelta a entrada
Move-Item data\xml_procesados\*.xml data\xml_entrada\
Move-Item data\xml_parciales\*.xml data\xml_entrada\
```

---

## 8. Errores Comunes y Soluciones

### 8.1 Error de conexion a BD

**Error:**
```
pyodbc.OperationalError: ('08001', '[08001] [Microsoft][SQL Server Native Client 11.0]
Named Pipes Provider: Could not open a connection to SQL Server')
```

**Causa:** SQL Server no esta accesible o las credenciales son incorrectas.

**Solucion:**
1. Verificar que SQL Server esta corriendo: `services.msc` -> SQL Server
2. Verificar `.env`: `DB_SERVER=localhost`, `DB_DATABASE=DBSAV71_TEST`
3. Verificar credenciales: `DB_USERNAME=devsav7`, `DB_PASSWORD=devsav7`
4. Verificar driver: `DB_DRIVER={SQL Server Native Client 11.0}`
5. Probar conexion manual:
   ```powershell
   python -c "import pyodbc; print(pyodbc.drivers())"
   ```
   Debe listar `SQL Server Native Client 11.0`.

### 8.2 Error al cargar catalogo

**Error:**
```
Error cargando catalogo de productos: ...
```

**Causa:** La tabla `SAVProducto` no es accesible o tiene una estructura diferente.

**Solucion:**
1. Verificar que la tabla existe:
   ```sql
   SELECT COUNT(*) FROM SAVProducto;
   -- Esperado: ~6,314
   ```
2. Verificar campos requeridos:
   ```sql
   SELECT TOP 1 Codigo, Nombre, Familia1Nombre, Unidad, CodigoSAT, PorcIva, Servicio
   FROM SAVProducto;
   ```

### 8.3 Error de parsing XML

**Error:**
```
Error parseando XML: ...
```

**Causas posibles:**
- XML malformado o truncado
- Encoding incorrecto (debe ser UTF-8)
- No es un CFDI valido (falta namespace)

**Solucion:**
1. Verificar que el XML abre correctamente en un editor de texto
2. Verificar que tiene el namespace CFDI:
   ```xml
   <cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" ...>
   ```
3. Si el XML es CFDI 3.3 en vez de 4.0, el parser lo detecta automaticamente

### 8.4 Error de matching

**Error:**
```
No se encontro match para: DESCRIPCION DEL PRODUCTO
```

**Causa:** El nombre del producto en el XML no coincide con ningun producto del catalogo ERP.

**Esto NO es un error** - es comportamiento esperado. El concepto se reporta en la hoja "Sin Match" del Excel.

**Para mejorar el matching:**
1. Verificar el umbral: `UMBRAL_MATCH_PRODUCTO=90` (bajar a 85 si hay muchos fallos)
2. Verificar que el proveedor tiene historial de compras:
   ```sql
   SELECT d.Producto, d.Nombre, COUNT(*) as Veces
   FROM SAVRecD d
   INNER JOIN SAVRecC c ON d.Serie = c.Serie AND d.NumRec = c.NumRec
   WHERE c.Proveedor = '001XXX' AND c.Serie IN ('R','F')
   GROUP BY d.Producto, d.Nombre
   ORDER BY Veces DESC;
   ```
3. Revisar el reporte Excel, hoja "Detalle Matching", para ver los scores y candidatos

### 8.5 Proveedor no encontrado

**Error:**
```
Proveedor no encontrado para RFC: XXXXXXXXXXXX
```

**Causa:** El RFC del emisor del XML no existe en `SAVProveedor`.

**Solucion:**
1. Verificar el RFC en la BD:
   ```sql
   SELECT Clave, Empresa, RFC FROM SAVProveedor WHERE RFC = 'XXXXXXXXXXXX';
   ```
2. Si no existe, el proveedor debe darse de alta manualmente en SAV7 antes de procesar ese XML
3. El XML se mueve a `data/xml_fallidos/`

### 8.6 UUID duplicado

**Error:**
```
UUID ya registrado: XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
```

**Causa:** La factura ya fue registrada previamente (por el agente o manualmente).

**Solucion:**
1. Verificar el registro existente:
   ```sql
   SELECT Serie, NumRec, Comprador, Fecha, Total
   FROM SAVRecC
   WHERE TimbradoFolioFiscal = 'UUID_AQUI' AND Serie = 'F';
   ```
2. Si fue registrado por error, revertir (ver seccion 7) y re-procesar
3. Si ya esta correctamente registrado, el XML se salta automaticamente

### 8.7 Error de venv / ModuleNotFoundError

**Error:**
```
ModuleNotFoundError: No module named 'pyodbc'
```

**Causa:** El venv no tiene acceso a los paquetes del sistema instalados via pacman.

**Solucion:**
1. Recrear el venv con `--system-site-packages`:
   ```powershell
   rm -r venv
   python -m venv --system-site-packages venv
   .\venv\bin\Activate.ps1
   pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt
   ```

2. Si el error es sobre `rapidfuzz`:
   ```powershell
   pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org rapidfuzz
   ```

### 8.8 Error de activacion del venv

**Error:**
```
.\venv\Scripts\Activate.ps1 : File not found
```

**Causa:** MSYS2 Python crea venvs con estructura Unix (`bin/` en vez de `Scripts/`).

**Solucion:**
```powershell
.\venv\bin\Activate.ps1
```

### 8.9 Error de pip install (SSL/compilacion)

**Error:**
```
pip._vendor.urllib3.exceptions.MaxRetryError: SSL: CERTIFICATE_VERIFY_FAILED
```
o
```
error: subprocess-exited-with-error - cmake not found
```

**Causa:** MSYS2 Python tiene problemas con certificados SSL y compilacion de extensiones C.

**Solucion:**
1. Para paquetes puros Python, usar `--trusted-host`:
   ```powershell
   pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org PAQUETE
   ```
2. Para paquetes con extensiones C (`pyodbc`, `lxml`), instalar via pacman:
   ```bash
   # En MSYS2 terminal (no PowerShell)
   pacman -S mingw-w64-x86_64-python-pyodbc
   pacman -S mingw-w64-x86_64-python-lxml
   ```
3. Recrear venv con `--system-site-packages` para que vea los paquetes de pacman

### 8.10 Error de permisos al mover XMLs

**Error:**
```
PermissionError: [WinError 32] The process cannot access the file because it is being used
```

**Causa:** Otro proceso tiene el XML abierto.

**Solucion:**
1. Cerrar cualquier editor o visor que tenga el XML abierto
2. Verificar que no hay otro proceso del agente corriendo

---

## 9. Referencia: Datos Existentes en BD

### 9.1 Registros manuales Serie F (Consolidacion=0) como referencia

Estos son registros que las capturistas crearon manualmente. Los registros del Agente3 deben tener la misma estructura.

**Registro con UUID (referencia ideal):**

| Campo | Valor (NumRec 67752) |
|-------|---------------------|
| Serie | F |
| Proveedor | 001563 |
| ProveedorNombre | PRODUCTOS TRESAN |
| Comprador | ABIGAIL RUIZ |
| Estatus | No Pagada |
| SubTotal1 | 72,606.40 |
| Iva | 0 |
| Total | 72,606.40 |
| Saldo | 72,606.40 |
| TimbradoFolioFiscal | a5d8f61e-7f55-4013-b354-e5a3b19bed1f |
| Consolidacion | 0 (false) |
| Articulos | 610 |
| Partidas | 2 |
| Paridad | 20 |
| Moneda | PESOS |
| Sucursal | 5 |
| NumOC | 0 |

**Registro con detalle (referencia de lineas):**

NumRec 67742 - ANTONIO EDUARDO QUIRINO SIFUENTES:

| Campo Encabezado | Valor |
|-------------------|-------|
| Total | 9,850.00 |
| TotalLetra | NUEVE MIL OCHOCIENTOS CINCUENTA PESOS 00/100 M.N. |
| Articulos | 200 |
| Partidas | 2 |
| MetododePago | PUE |

Detalle:

| Orden | Producto | Nombre | Cant | Costo | CostoImp | PorcIva | Unidad |
|-------|----------|--------|------|-------|----------|---------|--------|
| 1 | ABA002009 | HUEVO SAN JUAN 12 PZAS | 150 | 36 | 0 | 0 | PZA |
| 2 | ABA002008 | HUEVO SAN JUAN 30 PZAS | 50 | 89 | 0 | 0 | PZA |

> Nota: `Articulos = round(150 + 50) = 200`

### 9.2 Capturistas que crean registros manuales

Los registros del Agente3 reemplazan el trabajo manual de estas capturistas:

| Comprador | Registros |
|-----------|-----------|
| SALMA VAZQUEZ | 19,896 |
| LITZY CASTILLO | 5,479 |
| ABIGAIL RUIZ | 2,471 |
| Mireya Ale | 2,146 |
| ISAAC VILLANUEVA | 1,703 |

El Agente3 usa `Comprador = 'AGENTE3_SAT'` para distinguir sus registros.

### 9.3 Proveedores frecuentes sin remision

Estos proveedores tienen muchos registros manuales F (sin consolidacion) y son candidatos principales para el Agente3:

| Clave | Proveedor | Facturas F |
|-------|-----------|-----------|
| 001147 | COMERCIALIZADORA DE CEBOLLAS AG | 1,130 |
| 001017 | AGRODAK S.A DE C.V | 1,040 |
| 001297 | GRUPO KARYCY S.A DE C.V | 1,029 |
| 001640 | SERVICIO INTEGRAL DE SEGURIDAD | 895 |
| 001499 | OLGA GPE. ESPRONCEDA FLORES | 889 |

### 9.4 Estadisticas del catalogo

| Metrica | Valor |
|---------|-------|
| Total productos | 6,314 |
| Nombres unicos | 6,223 |
| Codigos SAT distintos | ~607 |

### 9.5 Siguiente NumRec disponible

```sql
SELECT ISNULL(MAX(NumRec), 0) + 1 AS SiguienteNumRec FROM SAVRecC WHERE Serie = 'F';
```

> **Valor al 12 Feb 2026:** `67768`
> Este numero se incrementa con cada registro. Anota el valor ANTES de cada prueba.

---

## Checklist Rapido de Pruebas

```
[ ] 1. Activar venv: .\venv\bin\Activate.ps1
[ ] 2. Verificar .env apunta a DBSAV71_TEST
[ ] 3. python main.py --test-conexion -> OK
[ ] 4. python main.py --explorar-catalogo -> ~6,314 productos
[ ] 5. Colocar XML(s) de prueba en data/xml_entrada/
[ ] 6. python main.py --dry-run -> Ver matching sin escribir
[ ] 7. Revisar reporte Excel generado
[ ] 8. Anotar siguiente NumRec disponible (consulta SQL)
[ ] 9. python main.py --archivo factura.xml -> Registro real
[ ] 10. Verificar en BD: SELECT * FROM SAVRecC WHERE Comprador='AGENTE3_SAT'
[ ] 11. Verificar detalle: SELECT * FROM SAVRecD WHERE NumRec=@creado
[ ] 12. Comparar con registro manual de referencia (67742 o 67752)
[ ] 13. Verificar consistencia de montos
[ ] 14. Si todo OK, probar con lote: python main.py
[ ] 15. Revertir datos de prueba (DELETE) si es necesario
```
