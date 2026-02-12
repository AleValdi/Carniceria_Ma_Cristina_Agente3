# Resultado de Pruebas - Agente 3

**Fecha:** 2026-02-12
**Ambiente:** DBSAV71_TEST
**Maquina:** macOS Darwin 25.2.0 (desarrollo remoto)
**Python:** 3.9+
**Driver ODBC:** ODBC Driver 17 for SQL Server

---

## Resumen Ejecutivo

| Metrica | Valor |
|---------|-------|
| Pruebas basicas | 2/2 OK |
| Dry-run individual | OK (AGRODAK 4/4) |
| Dry-run batch | OK (7/8 conceptos, 87.5%) |
| Registro real en TEST | 3/3 facturas registradas |
| Verificacion BD | 6/6 checks OK |
| Mejora de matching | 37.5% -> 87.5% (+50 pts) |

---

## Mejora de Matching Implementada

### Descripcion del Cambio

Se agrego un **Paso 2.5** al pipeline de matching entre el paso 2 (historial + token_sort_ratio) y el paso 3 (codigo SAT + fuzzy):

```
Paso 1: Match exacto por nombre normalizado (sin cambios)
Paso 2: Historial proveedor + token_sort_ratio (sin cambios)
Paso 2.5: Historial proveedor + token_set_ratio + desambiguacion por frecuencia (NUEVO)
Paso 3: Codigo SAT + fuzzy (sin cambios)
Paso 4: Catalogo completo + fuzzy (sin cambios)
```

### Por que token_set_ratio?

`token_sort_ratio` penaliza nombres abreviados porque ordena todos los tokens y compara caracter a caracter. `token_set_ratio` compara la interseccion de tokens, lo que maneja bien:

- "JALAPENO" vs "CHILE JALAPENO VERDE" -> token_set_ratio = 100 (JALAPENO esta contenido)
- "ZANAHORIA 10KG" vs "ZANAHORIA" -> token_set_ratio = 100

### Desambiguacion por Frecuencia

`token_set_ratio` genera ambiguedades (ej: "JALAPENO" matchea con CHILE JALAPENO VERDE y CHILE JALAPENO ROJO con score 100). Se resuelve con frecuencia historica de compra:

- Si el mejor tiene frecuencia >= 2x que el segundo -> aceptar
- Si no -> rechazar como ambiguo (comportamiento conservador)

Ejemplo real:
```
CHILE JALAPENO VERDE (score=100, freq=987) vs CHILE JALAPENO ROJO (score=100, freq=38)
-> 987 >= 2*38 (76) -> Acepta VERDE
```

### Cambios Realizados: Detalle Completo por Archivo

---

#### Cambio 1 de 5: `src/erp/models.py` — Campo de frecuencia en ProductoERP

**Linea modificada:** 23

**Codigo agregado:**
```python
# ANTES (linea 22):
    servicio: bool        # Flag de servicio

# DESPUES (lineas 22-23):
    servicio: bool        # Flag de servicio
    frecuencia: int = 0   # Veces comprado a un proveedor (para desambiguacion)
```

**Que es ProductoERP:**
`ProductoERP` es el dataclass central que representa un producto del catalogo SAVProducto de SAV7. Tiene 8 campos originales (codigo, nombre, familia1, familia2, unidad, codigo_sat, porc_iva, servicio) que se populan al cargar el catalogo completo (~6,314 productos) en `producto_repo.py`.

**Que hace el campo `frecuencia`:**
Almacena cuantas veces un proveedor especifico ha comprado ese producto historicamente (dato de SAVRecD). Se usa en el paso 2.5 del matcher para desambiguar cuando dos productos tienen el mismo score fuzzy: el que se compra mas frecuentemente gana.

**Por que `default=0`:**
El campo tiene `default=0` porque la mayoria de instancias de `ProductoERP` se crean sin frecuencia (al cargar el catalogo general). Solo se popula cuando se llama `obtener_productos_con_frecuencia()` para un proveedor especifico.

**Donde se usa ProductoERP (analisis de impacto):**

| Archivo | Como usa ProductoERP | Afectado? |
|---------|---------------------|-----------|
| `src/erp/producto_repo.py` | Crea instancias al cargar catalogo. No pasa `frecuencia`. | NO (usa default=0) |
| `src/matching/cache_productos.py` | Indexa por nombre, codigo, codigo_sat. No lee `frecuencia`. | NO |
| `src/matching/historial_compras.py` | `obtener_productos_proveedor()` devuelve ProductoERP del cache. | NO (metodo existente no toca frecuencia) |
| `src/matching/historial_compras.py` | **NUEVO** `obtener_productos_con_frecuencia()` crea copias con frecuencia populada. | SI (unico consumidor del campo) |
| `src/matching/producto_matcher.py` | Pasos 1-4 usan `.codigo`, `.nombre`. No leen `.frecuencia`. | NO |
| `src/matching/producto_matcher.py` | **NUEVO** Paso 2.5 lee `.frecuencia` para desambiguar. | SI (unico lector del campo) |
| `src/erp/registro_directo.py` | Lee `.codigo`, `.nombre`, `.unidad`, `.porc_iva` para INSERT. No lee `.frecuencia`. | NO |
| `src/reports/excel_generator.py` | Usa `.nombre`, `.codigo` para reportes. No lee `.frecuencia`. | NO |

**Riesgo:** BAJO. Campo aditivo con default, backward compatible. No rompe ningun codigo existente.

---

#### Cambio 2 de 5: `config/settings.py` — Nuevos settings de configuracion

**Lineas modificadas:** 57-59 (campos del dataclass) y 85-86 (lectura .env)

**Codigo agregado en el dataclass (lineas 57-59):**
```python
    # Configuracion de matching avanzado (paso 2.5: token_set + historial)
    habilitar_token_set_historial: bool = True  # Toggle para paso 2.5
    min_longitud_token_set: int = 5  # Min chars para activar token_set_ratio
```

**Codigo agregado en `from_env()` (lineas 85-86):**
```python
            habilitar_token_set_historial=os.getenv('HABILITAR_TOKEN_SET_HISTORIAL', 'true').lower() == 'true',
            min_longitud_token_set=int(os.getenv('MIN_LONGITUD_TOKEN_SET', '5')),
```

**Que es `Settings`:**
Es el dataclass global de configuracion del Agente 3. Se instancia una vez al inicio (`settings = Settings.from_env()` en linea 112) y se importa en multiples modulos. Lee valores del archivo `.env` con defaults seguros.

**Que hace cada campo nuevo:**

| Campo | Tipo | Default | Para que sirve |
|-------|------|---------|---------------|
| `habilitar_token_set_historial` | `bool` | `True` | **Toggle master** del paso 2.5. Si es `False`, el paso 2.5 no se ejecuta y el pipeline queda identico al original de 4 pasos. Permite desactivar la mejora sin tocar codigo si causa problemas en produccion. |
| `min_longitud_token_set` | `int` | `5` | Longitud minima del nombre normalizado para que entre al paso 2.5. Evita falsos positivos con nombres muy cortos como "SAL", "RON", "PAN" que matchearian con muchos productos via `token_set_ratio`. Con 5 chars: "EJOTE" (5) si entra, "SAL" (3) no. |

**Donde se leen estos campos:**

| Archivo | Linea | Que lee | Para que |
|---------|-------|---------|---------|
| `src/matching/producto_matcher.py` | 76 | `settings.habilitar_token_set_historial` | Guard para ejecutar o no el paso 2.5 |
| `src/matching/producto_matcher.py` | 255 | `settings.min_longitud_token_set` | Guard de longitud minima de nombre |

Ningun otro archivo lee estos campos.

**Variables de entorno correspondientes:**
```env
HABILITAR_TOKEN_SET_HISTORIAL=true   # o false para desactivar
MIN_LONGITUD_TOKEN_SET=5             # entero, min chars
```

Si no existen en `.env`, se usan los defaults (true y 5).

**Riesgo:** BAJO. Campos nuevos con defaults seguros, lectura pasiva, no afectan codigo existente.

---

#### Cambio 3 de 5: `src/matching/historial_compras.py` — Nuevo metodo con frecuencia

**Lineas modificadas:** 31-32 (cache nuevo), 89-148 (metodo nuevo), 152-153 (limpiar_cache actualizado)

**Codigo agregado — cache nuevo (linea 32):**
```python
        # Cache separado para productos con frecuencia populada
        self._historial_freq_cache: Dict[str, List[ProductoERP]] = {}
```

**Codigo agregado — metodo nuevo (lineas 89-148):**
```python
    def obtener_productos_con_frecuencia(self, clave_proveedor: str) -> List[ProductoERP]:
        """
        Obtener productos con el campo frecuencia populado.
        Similar a obtener_productos_proveedor pero incluye veces_comprado.
        """
        if clave_proveedor in self._historial_freq_cache:
            return self._historial_freq_cache[clave_proveedor]

        query = f"""
            SELECT d.Producto, d.Nombre, COUNT(*) as veces_comprado
            FROM {self.config.tabla_detalle_recepciones} d
            INNER JOIN {self.config.tabla_recepciones} c
                ON d.Serie = c.Serie AND d.NumRec = c.NumRec
            WHERE c.Proveedor = ?
              AND c.Serie IN ('R', 'F')
              AND d.Producto IS NOT NULL
              AND d.Producto != ''
            GROUP BY d.Producto, d.Nombre
            ORDER BY veces_comprado DESC
        """

        try:
            resultados = self.connector.execute_custom_query(query, (clave_proveedor,))

            productos = []
            for row in resultados:
                codigo = row['Producto'].strip() if row['Producto'] else ''
                if not codigo:
                    continue

                frecuencia = row.get('veces_comprado', 0)

                if self.cache:
                    producto_base = self.cache.buscar_por_codigo(codigo)
                    if producto_base:
                        from dataclasses import replace
                        producto_con_freq = replace(producto_base, frecuencia=frecuencia)
                        productos.append(producto_con_freq)

            self._historial_freq_cache[clave_proveedor] = productos
            # ... logging y error handling
```

**Codigo modificado — limpiar_cache (linea 153):**
```python
    def limpiar_cache(self):
        """Limpiar cache interno de historial"""
        self._historial_cache.clear()
        self._historial_freq_cache.clear()  # <-- LINEA AGREGADA
```

**Que es `HistorialCompras`:**
Clase que consulta la BD para obtener los productos que un proveedor ha comprado historicamente. Reduce el espacio de busqueda del matching de ~6,314 productos a los ~10-50 que el proveedor realmente compra. Se instancia en `main.py` (linea 263) y se pasa al `ProductoMatcher`.

**Diferencia entre los dos metodos:**

| Aspecto | `obtener_productos_proveedor()` (existente) | `obtener_productos_con_frecuencia()` (nuevo) |
|---------|---------------------------------------------|----------------------------------------------|
| Query SQL | Identica | Identica |
| Retorna ProductoERP con frecuencia? | NO (frecuencia=0, default) | SI (frecuencia=veces_comprado) |
| Modifica ProductoERP original? | NO (obtiene del cache) | NO (usa `dataclasses.replace()` para crear copias) |
| Cache | `_historial_cache` | `_historial_freq_cache` (separado) |
| Quien lo llama | Paso 2 del matcher | Paso 2.5 del matcher |
| Existe desde | Version original | Esta version |

**Por que `dataclasses.replace()` y no mutacion directa:**
```python
# CORRECTO: crea copia, no muta el original del cache principal
producto_con_freq = replace(producto_base, frecuencia=frecuencia)

# INCORRECTO (lo que NO se hace): mutaria el objeto compartido en CacheProductos
# producto_base.frecuencia = frecuencia  # PELIGRO: afectaria otros usos
```
Esto es critico porque `producto_base` viene de `CacheProductos` que es compartido por todos los pasos del matcher. Si se mutara el original, los pasos 1-4 verian datos contaminados.

**Por que un cache separado (`_historial_freq_cache`):**
El metodo existente `obtener_productos_proveedor()` tiene su propio cache `_historial_cache` con ProductoERP sin frecuencia. No se puede reutilizar porque los objetos son distintos (frecuencia=0 vs frecuencia=N). Tener caches separados evita que un metodo interfiera con el otro.

**Impacto en performance:**
La query SQL es la misma que la del metodo existente. Se ejecuta una vez por proveedor y se cachea. Si el paso 2 ya hizo la consulta, el paso 2.5 hace otra consulta identica para el mismo proveedor. Esto podria optimizarse en el futuro compartiendo resultados, pero el costo es minimo (1 query adicional, ~50ms) y mantiene la separacion de responsabilidades.

**Riesgo:** BAJO. Metodo nuevo aislado. No modifica `obtener_productos_proveedor()` ni el cache existente.

---

#### Cambio 4 de 5: `src/matching/producto_matcher.py` — Paso 2.5 del pipeline (CAMBIO CRITICO)

**Lineas modificadas:** 68, 75-81, 244-349

Este es el cambio mas importante y el unico con riesgo MEDIO porque modifica el flujo principal del matching.

**Cambio A — Llamada al paso 2.5 en `matchear_concepto()` (lineas 75-81):**

```python
        # Paso 2: Historial del proveedor + fuzzy (token_sort_ratio)  # <-- comentario actualizado
        resultado = self._match_historial_proveedor(
            concepto, nombre_normalizado, clave_proveedor
        )
        if resultado.matcheado:
            return resultado

        # NUEVO: Paso 2.5: Historial del proveedor + token_set_ratio + frecuencia
        if settings.habilitar_token_set_historial:
            resultado = self._match_historial_token_set(
                concepto, nombre_normalizado, clave_proveedor
            )
            if resultado.matcheado:
                return resultado

        # Paso 3: Codigo SAT + fuzzy  # <-- sin cambios desde aqui
```

**Por que entre paso 2 y paso 3:**
- Paso 2 usa `token_sort_ratio` contra historial del proveedor. Es mas estricto y preciso.
- Si paso 2 falla (nombres abreviados), paso 2.5 intenta con `token_set_ratio` (mas permisivo pero con desambiguacion).
- Solo si ambos fallan, se continua con paso 3 (codigo SAT) y paso 4 (catalogo global).
- Si `habilitar_token_set_historial=False`, se salta directamente a paso 3 (comportamiento original).

**Cambio B — Metodo `_match_historial_token_set()` (lineas 244-278):**

```python
    def _match_historial_token_set(
        self,
        concepto: Concepto,
        nombre_normalizado: str,
        clave_proveedor: str
    ) -> ResultadoMatchProducto:
        """
        Paso 2.5: Historial del proveedor + token_set_ratio + desambiguacion por frecuencia.
        Maneja nombres abreviados (ej: 'JALAPENO' vs 'CHILE JALAPENO VERDE').
        """
        # Guard 1: rechazar nombres muy cortos (evita "SAL" matcheando con todo)
        if len(nombre_normalizado) < settings.min_longitud_token_set:
            return ResultadoMatchProducto(concepto_xml=concepto)

        # Obtener productos CON frecuencia populada (usa cache separado)
        productos_freq = self.historial.obtener_productos_con_frecuencia(clave_proveedor)
        if not productos_freq:
            return ResultadoMatchProducto(concepto_xml=concepto)

        # Fuzzy matching con token_set_ratio + desambiguacion
        mejor, candidatos = self._fuzzy_match_token_set(
            nombre_normalizado, productos_freq, self.umbral_match  # umbral=90
        )

        if mejor:
            producto, score = mejor
            return ResultadoMatchProducto(
                concepto_xml=concepto,
                producto_erp=producto,
                confianza=score / 100.0,
                nivel_confianza="ALTA",
                metodo_match="historial_token_set",  # nuevo identificador de metodo
                candidatos_descartados=candidatos,
                mensaje=f"Match historial token_set: {producto.codigo} (score {score}, freq {producto.frecuencia})"
            )

        return ResultadoMatchProducto(concepto_xml=concepto)
```

**Logica del metodo:**
1. Guard de longitud: si el nombre normalizado tiene menos de `min_longitud_token_set` (5) caracteres, retorna NO_MATCH.
2. Obtiene productos del proveedor con frecuencia populada.
3. Si el proveedor no tiene historial, retorna NO_MATCH.
4. Llama a `_fuzzy_match_token_set()` (ver abajo) con umbral=90.
5. Si hay match, retorna con `metodo_match="historial_token_set"` y `nivel_confianza="ALTA"`.

**Cambio C — Metodo `_fuzzy_match_token_set()` (lineas 280-349):**

```python
    def _fuzzy_match_token_set(
        self,
        nombre_normalizado: str,
        productos: List[ProductoERP],
        umbral: int
    ) -> Tuple[Optional[Tuple[ProductoERP, int]], int]:
        """
        Fuzzy matching usando token_set_ratio con desambiguacion por frecuencia.
        """
        candidatos: List[Tuple[ProductoERP, int]] = []

        for producto in productos:
            nombre_prod = self.cache.obtener_nombre_normalizado(producto.codigo)
            if not nombre_prod:
                nombre_prod = normalizar_texto(producto.nombre)

            # DIFERENCIA CLAVE: usa token_set_ratio en vez de token_sort_ratio
            score = fuzz.token_set_ratio(nombre_normalizado, nombre_prod)

            if score >= umbral:
                candidatos.append((producto, score))

        if not candidatos:
            return None, 0

        # Ordenar por (score DESC, frecuencia DESC)
        candidatos.sort(key=lambda x: (x[1], x[0].frecuencia), reverse=True)

        mejor_producto, mejor_score = candidatos[0]

        # Detectar ambiguedad con desambiguacion por frecuencia
        if len(candidatos) > 1:
            segundo_producto, segundo_score = candidatos[1]
            gap = mejor_score - segundo_score

            if gap < self.margen_ambiguedad:  # gap < 5 puntos = empate
                freq_mejor = mejor_producto.frecuencia
                freq_segundo = segundo_producto.frecuencia

                # Desambiguar: acepta SOLO si frecuencia >= 2x
                if freq_mejor > 0 and freq_segundo > 0:
                    if freq_mejor >= 2 * freq_segundo:
                        logger.info(f"Token_set desambiguado por frecuencia: ...")
                        return (mejor_producto, mejor_score), len(candidatos) - 1

                # No se puede desambiguar: rechazar como ambiguo
                logger.warning(f"Token_set ambiguo para '{nombre_normalizado}': ...")
                return None, len(candidatos)

        # Sin ambiguedad, pero verificar que tiene frecuencia > 0
        if mejor_producto.frecuencia == 0:
            return None, len(candidatos)

        return (mejor_producto, mejor_score), len(candidatos) - 1
```

**Diferencia entre `token_sort_ratio` (paso 2) y `token_set_ratio` (paso 2.5):**

| Aspecto | `token_sort_ratio` (paso 2) | `token_set_ratio` (paso 2.5) |
|---------|---------------------------|----------------------------|
| Algoritmo | Ordena tokens alfabeticamente y compara string completo | Compara interseccion, diferencia y union de tokens |
| "JALAPENO" vs "CHILE JALAPENO VERDE" | ~57 (penaliza tokens faltantes) | 100 (JALAPENO esta contenido) |
| "ZANAHORIA 10KG" vs "ZANAHORIA" | ~73 (penaliza "10KG" extra) | 100 (ZANAHORIA esta contenida) |
| "EJOTE" vs "EJOTE" | 100 | 100 |
| Falsos positivos | Bajo | **Alto** (necesita desambiguacion) |
| Usado en paso | 2 (metodo `_fuzzy_match_lista`) | 2.5 (metodo `_fuzzy_match_token_set`) |

**Logica de desambiguacion (el corazon del cambio):**

Cuando `token_set_ratio` encuentra multiples candidatos con scores similares (gap < 5 puntos), necesita decidir cual es el correcto. Ejemplo real:

```
XML dice: "JALAPENO"
Candidato A: CHILE JALAPENO VERDE  (score=100, frecuencia=987)
Candidato B: CHILE JALAPENO ROJO   (score=100, frecuencia=38)
```

Ambos tienen score 100 (empate). La regla es:
- `987 >= 2 * 38` (987 >= 76)? **SI** -> Acepta CHILE JALAPENO VERDE
- Razon: el proveedor ha vendido VERDE 987 veces vs ROJO solo 38. Es 26x mas frecuente. Es una senal fuerte de que el proveedor se refiere al VERDE.

Si la frecuencia NO fuera >= 2x:
```
Candidato A: CHILE JALAPENO VERDE  (score=100, frecuencia=50)
Candidato B: CHILE JALAPENO ROJO   (score=100, frecuencia=40)
```
- `50 >= 2 * 40` (50 >= 80)? **NO** -> Rechaza como ambiguo (NO_MATCH)
- Razon: las frecuencias son muy similares, no hay senal clara. Es mejor reportarlo para revision manual que adivinar.

**Guards de seguridad completos del paso 2.5:**

| Guard | Condicion | Que previene |
|-------|-----------|-------------|
| Toggle global | `settings.habilitar_token_set_historial == False` | Desactiva paso 2.5 completamente |
| Longitud minima | `len(nombre_normalizado) < 5` | Falsos positivos con "SAL", "RON", "PAN" |
| Sin historial | `productos_freq` vacio | No ejecutar sin datos del proveedor |
| Frecuencia cero | `mejor_producto.frecuencia == 0` | No aceptar sin evidencia historica |
| Empate sin ratio 2x | `freq_mejor < 2 * freq_segundo` | No adivinar entre candidatos similares |

**Nuevo `metodo_match` retornado: `"historial_token_set"`:**
Este valor aparece en `ResultadoMatchProducto.metodo_match` y se muestra en:
- Logs de consola (linea 124: `via historial_token_set`)
- Reporte Excel, hoja "Detalle Matching" (columna metodo)
- No requiere cambios en `excel_generator.py` porque ya maneja cualquier string en ese campo

**Riesgo:** MEDIO. Modifica el flujo principal de `matchear_concepto()`, pero:
- Solo agrega un paso intermedio (no modifica pasos existentes)
- Es desactivable con `HABILITAR_TOKEN_SET_HISTORIAL=false`
- Tiene 5 guards de seguridad
- Los metodos nuevos son privados (`_match_historial_token_set`, `_fuzzy_match_token_set`)

---

#### Cambio 5 de 5: `.env.example` — Documentacion de nuevas variables

**Lineas agregadas:** 29-34

```env
# --- Matching Avanzado (Paso 2.5: token_set + historial) ---
# Habilitar paso 2.5: token_set_ratio con desambiguacion por frecuencia historica
HABILITAR_TOKEN_SET_HISTORIAL=true

# Longitud minima de nombre para activar token_set_ratio
MIN_LONGITUD_TOKEN_SET=5
```

**Que afecta:** Solo documentacion para otros desarrolladores. No afecta ejecucion.

**Riesgo:** NULO.

---

### Archivos NO Modificados

Estos archivos **no se tocaron**. Siguen funcionando identico a como estaban antes de los cambios:

| Archivo | Funcion | Por que no se toco |
|---------|---------|-------------------|
| `main.py` | CLI y orquestacion | No requiere cambios. Instancia `ProductoMatcher` igual que antes; el paso 2.5 se auto-configura desde `settings`. |
| `src/erp/registro_directo.py` | INSERT Serie F en BD | La logica de registro es independiente del matching. Recibe `ResultadoMatchProducto` y solo lee `.producto_erp.codigo`, `.nombre`, `.unidad`, `.porc_iva`. No lee `.frecuencia`. |
| `src/erp/sav7_connector.py` | Conexion pyodbc | Capa de BD generica, no tiene logica de negocio. |
| `src/erp/proveedor_repo.py` | Busqueda proveedor por RFC | Retorna `ProveedorERP`, no usa `ProductoERP`. |
| `src/erp/producto_repo.py` | Carga catalogo SAVProducto | Crea `ProductoERP` sin `frecuencia` (usa default=0). No necesita cambios. |
| `src/erp/utils.py` | `numero_a_letra()` | Conversion de montos a texto. Sin relacion con matching. |
| `src/matching/cache_productos.py` | Indices en memoria | Indexa por nombre/codigo/codigo_sat. No indexa por frecuencia. No necesita cambios. |
| `src/sat/xml_parser.py` | Parser CFDI XML | Lee XMLs y retorna `Factura`/`Concepto`. Sin relacion con matching. |
| `src/sat/models.py` | `Factura`, `Concepto`, `TipoComprobante` | Modelos SAT inmutables. |
| `src/reports/excel_generator.py` | Reporte Excel 6 hojas | Lee `ResultadoMatchProducto.metodo_match` como string generico. Cualquier nuevo valor (como `"historial_token_set"`) se muestra automaticamente sin cambios. |
| `config/database.py` | `DatabaseConfig`, `DatabaseConnection` | Configuracion y conexion SQL Server. Sin relacion con matching. |

---

### Diagrama de Dependencias de los Cambios

```
.env / .env.example
  |
  v
config/settings.py  (lee HABILITAR_TOKEN_SET_HISTORIAL, MIN_LONGITUD_TOKEN_SET)
  |
  v
src/matching/producto_matcher.py  (usa settings.habilitar_token_set_historial)
  |   |
  |   +-- _match_historial_token_set()    (metodo nuevo, paso 2.5)
  |   |     |
  |   |     +-- self.historial.obtener_productos_con_frecuencia()
  |   |     |     |
  |   |     |     +-- src/matching/historial_compras.py  (metodo nuevo)
  |   |     |           |
  |   |     |           +-- dataclasses.replace(producto_base, frecuencia=N)
  |   |     |                 |
  |   |     |                 +-- src/erp/models.py  (campo frecuencia nuevo)
  |   |     |
  |   |     +-- self._fuzzy_match_token_set()  (metodo nuevo)
  |   |           |
  |   |           +-- fuzz.token_set_ratio()  (de rapidfuzz, ya instalado)
  |   |           +-- producto.frecuencia  (lee campo nuevo)
  |   |
  |   +-- _match_exacto()           (SIN CAMBIOS)
  |   +-- _match_historial_proveedor()  (SIN CAMBIOS)
  |   +-- _match_codigo_sat()        (SIN CAMBIOS)
  |   +-- _match_catalogo_completo() (SIN CAMBIOS)
  |   +-- _fuzzy_match_lista()       (SIN CAMBIOS)
  |
  v
Resultado: metodo_match = "historial_token_set" (se propaga a Excel sin cambios)
```

---

### Toggle de Desactivacion Rapida

Si el paso 2.5 causa problemas en produccion (falsos positivos, regresiones, performance):

```env
# En el archivo .env, agregar o cambiar a:
HABILITAR_TOKEN_SET_HISTORIAL=false
```

Reiniciar el agente. El paso 2.5 se salta completamente y el pipeline vuelve al comportamiento original de 4 pasos. No requiere tocar codigo ni reiniciar la BD.

---

### Rollback Completo (si se necesita eliminar los cambios del codigo)

Revertir en orden inverso de implementacion:

**Paso 1: `src/matching/producto_matcher.py`**
- Eliminar metodos `_match_historial_token_set()` (lineas 244-278) y `_fuzzy_match_token_set()` (lineas 280-349)
- Eliminar lineas 75-81 (llamada al paso 2.5 en `matchear_concepto()`)
- Restaurar comentario de paso 2 a "Paso 2: Historial del proveedor + fuzzy"

**Paso 2: `src/matching/historial_compras.py`**
- Eliminar metodo `obtener_productos_con_frecuencia()` (lineas 89-148)
- Eliminar linea 32 (`_historial_freq_cache`)
- En `limpiar_cache()`: quitar `self._historial_freq_cache.clear()`

**Paso 3: `config/settings.py`**
- Eliminar lineas 57-59 (campos `habilitar_token_set_historial` y `min_longitud_token_set`)
- Eliminar lineas 85-86 (lectura de `.env` en `from_env()`)

**Paso 4: `src/erp/models.py`**
- Eliminar linea 23 (`frecuencia: int = 0`)

**Paso 5: `.env.example`**
- Eliminar lineas 29-34 (seccion Matching Avanzado)

**Verificacion post-rollback:**
```bash
python3 main.py --dry-run --archivo 0801DD85-6D1D-4A6D-B3AB-673547AFCFC7.xml
# Debe dar 3/4 (AGRODAK) = comportamiento original sin paso 2.5
```

---

## Seccion 3: Pruebas Basicas

### 3.1 Test de Conexion

```
$ python3 main.py --test-conexion
```

**Resultado:** OK

| Tabla | Registros |
|-------|-----------|
| SAVRecC (Recepciones) | 111,181 |
| SAVRecD (Detalle) | 440,745 |
| SAVProveedor | 968 |
| SAVProducto | 6,314 |
| Facturas Serie F | 65,453 |

### 3.2 Explorar Catalogo

```
$ python3 main.py --explorar-catalogo
```

**Resultado:** OK

| Metrica | Valor |
|---------|-------|
| Total productos | 6,314 |
| Nombres unicos | 6,217 |
| Codigos SAT unicos | 606 |
| Con IVA | 2,074 |
| Sin IVA | 4,240 |
| Servicios | 102 |

Top 5 familias: ABARROTES (1,597), INSUMOS (972), BEBIDAS (623), LACTEOS (486), PAN Y REPOSTERIA (451)

---

## Seccion 4: Pruebas Dry-Run

### 4.1 Dry-Run Individual (AGRODAK)

```
$ python3 main.py --dry-run --archivo 0801DD85-6D1D-4A6D-B3AB-673547AFCFC7.xml
```

**Resultado:** 4/4 conceptos matcheados (EXITOSO)

| Concepto XML | Producto ERP | Metodo | Score |
|-------------|-------------|--------|-------|
| CHILE CHILACA | CHILE CHILACA (FYV002023) | exacto | 100% |
| EJOTE | EJOTE (FYV002038) | exacto | 100% |
| JENGIBRE | JENGIBRE (ABA007259) | exacto | 100% |
| ZANAHORIA 10KG | ZANAHORIA (FYV002070) | **historial_token_set** | **100%** |

La ZANAHORIA 10KG antes fallaba con token_sort_ratio (score < 90) porque "10KG" penalizaba. Con token_set_ratio matchea perfecto.

### 4.2 Dry-Run Batch (3 XMLs)

```
$ python3 main.py --dry-run
```

**Resultado:** 3 facturas procesadas, 7/8 conceptos (87.5%)

| Factura | Emisor | Conceptos | Match | Resultado |
|---------|--------|-----------|-------|-----------|
| F-94704 | AGRODAK | 4 | 4/4 | EXITOSO |
| 02BE4812 | ANTONIO EDUARDO QUIRINO | 2 | 1/2 | PARCIAL |
| 006A-458843 | AGRO CHILEROS | 2 | 2/2 | EXITOSO |

**Detalle AGROCHILEROS (desambiguacion por frecuencia):**

| Concepto XML | Candidato 1 | Candidato 2 | Decision |
|-------------|------------|------------|----------|
| JALAPENO | CHILE JALAPENO VERDE (score=100, freq=987) | CHILE JALAPENO ROJO (score=100, freq=38) | VERDE (987 >= 2*38) |
| SERRANO | CHILE SERRANO (score=100, freq=922) | BULTO CHILE SERRANO (score=100, freq=1) | CHILE SERRANO (922 >= 2*1) |

**QUIRINO (caso documentado de limitacion):**

| Concepto XML | Match? | Razon |
|-------------|--------|-------|
| HUEVO BLANCO | HUEVO SAN JUAN (matcheo) | token_set: "HUEVO" en comun, sin ambiguedad |
| HUEVO 30PZ | NO MATCH | Nombre demasiado generico, multiples candidatos HUEVO |

### Comparacion Antes/Despues

| Factura | Antes (sin paso 2.5) | Despues (con paso 2.5) |
|---------|---------------------|----------------------|
| AGRODAK | 3/4 (75%) | **4/4 (100%)** |
| AGROCHILEROS | 0/2 (0%) | **2/2 (100%)** |
| QUIRINO | 0/2 (0%) | **1/2 (50%)** |
| **Total** | **3/8 (37.5%)** | **7/8 (87.5%)** |

**Mejora: +50 puntos porcentuales (37.5% -> 87.5%)**

> Nota: Se esperaba 6/8 (75%) en el plan. El resultado real fue 7/8 (87.5%) porque QUIRINO matcheo 1 concepto (HUEVO BLANCO -> HUEVO SAN JUAN).

---

## Seccion 5: Registro Real en TEST

### 5.1 Verificacion de Ambiente

```
DB_DATABASE=DBSAV71_TEST
DB_SERVER=<servidor>
```

### 5.2 NumRec Antes del Registro

```sql
SELECT ISNULL(MAX(NumRec), 0) + 1 FROM SAVRecC WHERE Serie = 'F';
-- Resultado: 67768
```

Registros AGENTE3_SAT existentes antes: **0**

### 5.3 Registro Ejecutado

```
$ python3 main.py
```

| NumRec | Emisor | Total | Conceptos | Resultado |
|--------|--------|-------|-----------|-----------|
| F-67768 | AGRODAK | $1,900.00 | 4/4 | EXITOSO |
| F-67769 | QUIRINO | $3,240.00 | 1/2 | PARCIAL |
| F-67770 | AGROCHILEROS | $946.40 | 2/2 | EXITOSO |

Duracion total: 5.0 segundos

---

## Seccion 6: Verificacion en BD

### 6.1 Registros AGENTE3_SAT Creados

```sql
SELECT Serie, NumRec, ProveedorNombre, Total, Estatus, Articulos, Partidas,
       TimbradoFolioFiscal, Consolidacion
FROM SAVRecC
WHERE Comprador = 'AGENTE3_SAT' AND Serie = 'F'
ORDER BY NumRec;
```

| NumRec | Proveedor | Total | Estatus | Articulos | Partidas | UUID (primeros 12) |
|--------|-----------|-------|---------|-----------|----------|--------------------|
| 67768 | AGRODAK | $1,900.00 | No Pagada | 30 | 4 | 0801DD85-6D1D |
| 67769 | QUIRINO | $3,240.00 | No Pagada | 3 | 1 | 02BE4812-81CE |
| 67770 | AGROCHILEROS | $946.40 | No Pagada | 57 | 2 | 69DC012F-0750 |

Todos con `Consolidacion = 0` (correcto para registro directo).

### 6.3 Detalle (SAVRecD)

**F-67768 (AGRODAK - 4 lineas):**

| Orden | Producto | Nombre | Cantidad | Costo | IVA | Unidad |
|-------|----------|--------|----------|-------|-----|--------|
| 1 | FYV002023 | CHILE CHILACA | 6.00 | $40.00 | 0% | KG |
| 2 | FYV002038 | EJOTE | 10.00 | $28.00 | 0% | KG |
| 3 | ABA007259 | JENGIBRE | 2.00 | $90.00 | 0% | KG |
| 4 | FYV002070 | ZANAHORIA | 12.00 | $100.00 | 0% | KG |

**F-67769 (QUIRINO - 1 linea, parcial):**

| Orden | Producto | Nombre | Cantidad | Costo | IVA | Unidad |
|-------|----------|--------|----------|-------|-----|--------|
| 1 | ABA002001 | HUEVO SAN JUAN | 3.00 | $1,080.00 | 0% | KG |

**F-67770 (AGROCHILEROS - 2 lineas):**

| Orden | Producto | Nombre | Cantidad | Costo | IVA | Unidad |
|-------|----------|--------|----------|-------|-----|--------|
| 1 | FYV002025 | CHILE JALAPENO VERDE | 29.40 | $16.00 | 0% | KG |
| 2 | FYV002031 | CHILE SERRANO | 28.00 | $17.00 | 0% | KG |

### 6.4 UUID Unico

| UUID | Estado |
|------|--------|
| 0801DD85-6D1D-4A6D-B3AB-673547AFCFC7 | OK (unico) |
| 02BE4812-81CE-4A6A-B0B3-CF1C24B56DD7 | OK (unico) |
| 69DC012F-0750-11F1-A02F-B9605063AF08 | OK (unico) |

### 6.5 Consistencia de Montos

| NumRec | SubTotal (enc) | SubTotal (det) | IVA (enc) | IVA (det) | Estado |
|--------|---------------|----------------|-----------|-----------|--------|
| 67768 | $1,900.00 | $1,900.00 | $0.00 | $0.00 | OK |
| 67769 | $3,240.00 | $3,240.00 | $0.00 | $0.00 | OK |
| 67770 | $946.40 | $946.40 | $0.00 | $0.00 | OK |

### 6.6 Comparacion con Registro Manual (PRODUCCION)

Campos estructurales comparados con F-67752 (PRODUCTOS TRESAN, registro manual):

| Campo | PRODUCCION (F-67752) | TEST (F-67768) | OK? |
|-------|---------------------|----------------|-----|
| Consolidacion | 0 | 0 | OK |
| Paridad | 20.00 | 20.00 | OK |
| Moneda | PESOS | PESOS | OK |
| MetodoPago | PPD | PPD | OK |
| Sucursal | 5 | 5 | OK |
| NumOC | 0 | 0 | OK |
| TipoRecepcion | COMPRAS | COMPRAS | OK |
| Afectacion | TIENDA | TIENDA | OK |
| Departamento | NA | TIENDA | Nota* |

> *Nota: El campo Departamento tiene valor 'NA' en algunos registros manuales y 'TIENDA' en otros. No es una diferencia critica.

---

## Registros en Base de Datos

Los 3 registros se mantienen en `DBSAV71_TEST` para referencia futura.

### Consultar Registros

```sql
-- Ver todos los registros del Agente 3
SELECT Serie, NumRec, ProveedorNombre, Total, Estatus, Articulos, Partidas,
       TimbradoFolioFiscal, Comprador, Fecha
FROM SAVRecC
WHERE Comprador = 'AGENTE3_SAT' AND Serie = 'F'
ORDER BY NumRec;

-- Ver detalle de un registro especifico
SELECT Orden, Producto, Nombre, Cantidad, Costo, PorcIva, Unidad
FROM SAVRecD
WHERE Serie = 'F' AND NumRec = 67768  -- Cambiar NumRec segun necesidad
ORDER BY Orden;
```

### Eliminar Registros (si se necesita revertir)

```sql
-- IMPORTANTE: Ejecutar SOLO en DBSAV71_TEST
-- Paso 1: Eliminar detalle PRIMERO (FK logica)
DELETE FROM SAVRecD WHERE Serie = 'F' AND NumRec IN (67768, 67769, 67770);

-- Paso 2: Eliminar encabezados
DELETE FROM SAVRecC WHERE Serie = 'F' AND NumRec IN (67768, 67769, 67770);

-- Verificar limpieza
SELECT COUNT(*) FROM SAVRecC WHERE Comprador = 'AGENTE3_SAT';
-- Debe dar 0
```

### Eliminar TODOS los registros del Agente 3

```sql
-- PRECAUCION: Elimina TODO lo creado por AGENTE3_SAT
-- Paso 1: Obtener NumRecs
SELECT NumRec FROM SAVRecC WHERE Comprador = 'AGENTE3_SAT' AND Serie = 'F';

-- Paso 2: Eliminar detalle
DELETE d FROM SAVRecD d
INNER JOIN SAVRecC c ON d.Serie = c.Serie AND d.NumRec = c.NumRec
WHERE c.Comprador = 'AGENTE3_SAT' AND c.Serie = 'F';

-- Paso 3: Eliminar encabezados
DELETE FROM SAVRecC WHERE Comprador = 'AGENTE3_SAT' AND Serie = 'F';
```

### Mover XMLs de Vuelta (si se revierten)

Los XMLs fueron movidos a:
- `data/xml_procesados/` (AGRODAK, AGROCHILEROS)
- `data/xml_parciales/` (QUIRINO)

Para revertir:
```bash
mv data/xml_procesados/*.xml data/xml_entrada/
mv data/xml_parciales/*.xml data/xml_entrada/
```

---

## Reporte Excel Generado

Ruta: `data/reportes/registro_directo_20260212_140352.xlsx`

Contiene 6 hojas:
1. **Resumen** - 3 procesadas, 2 exitosas, 1 parcial, 0 fallidas
2. **Exitosos** - F-67768 (AGRODAK), F-67770 (AGROCHILEROS)
3. **Parciales** - F-67769 (QUIRINO, 1/2 conceptos)
4. **No Registradas** - (vacia)
5. **Sin Match** - 1 concepto: HUEVO 30PZ de QUIRINO
6. **Detalle Matching** - 8 conceptos con metodo y score

---

## Problemas Encontrados y Soluciones

### 1. OpenSSL 3.x en macOS con ODBC Driver 18

**Problema:** Error de conexion SSL al usar ODBC Driver 18 en macOS.
**Solucion:** El archivo `config/database.py` ya incluye un workaround que crea un `openssl.cnf` temporal con settings legacy. Funcionando correctamente.

### 2. Sin problemas adicionales

Todas las pruebas pasaron sin errores. La conexion via Tailscale es estable.

---

## Conclusion

La mejora de matching (paso 2.5 con `token_set_ratio` + desambiguacion por frecuencia) supero las expectativas:

- **Esperado:** 6/8 conceptos (75%)
- **Real:** 7/8 conceptos (87.5%)
- **Mejora absoluta:** +4 conceptos respecto al baseline de 3/8

El unico concepto que no matcheo ("HUEVO 30PZ") corresponde a un caso de limitacion documentada donde el nombre del XML es fundamentalmente diferente al del catalogo ERP y genera ambiguedad con multiples productos HUEVO.

Los 3 registros en TEST (F-67768, F-67769, F-67770) fueron verificados contra registros manuales de produccion y tienen estructura consistente.
