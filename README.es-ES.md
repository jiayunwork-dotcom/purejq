

# purejq

[![CI](https://github.com/adam2go/purejq/actions/workflows/ci.yml/badge.svg)](https://github.com/adam2go/purejq/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/purejq)](https://pypi.org/project/purejq/)
[![Python](https://img.shields.io/badge/python-3.9%E2%80%933.14%20%7C%20PyPy-blue)](.github/workflows/ci.yml)
[![Conformance](https://img.shields.io/badge/jq%20test%20suite-96.2%25-brightgreen)](tests/conformance/expected_failures.txt)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**[jq](https://jqlang.github.io/jq/), como una biblioteca pura de Python.** Sin extensión en C, sin
binario: si Python se ejecuta, purejq se ejecuta — Pyodide/WASM, entornos aislados (sandbox), Lambda,
cualquier lugar donde `pip install` sea todo lo que tienes.

![demo de purejq](demo/purejq.gif)

```sh
pip install purejq
```

```python
import purejq

purejq.first(".users[] | select(.age > 26) | .name", data)   # trabaja directamente con tus diccionarios
prog = purejq.compile("group_by(.team) | map(length)")        # compila una vez, ejecuta muchas veces
prog.first(batch)
```

```sh
echo '{"a":[1,2,3]}' | purejq '.a | map(. * 2)'               # CLI familiar, mismos parámetros
```

## ¿Por qué purejq

- **¿Integrar jq en Python? purejq es de 6 a 40 veces más rápido que las vinculaciones en C.**
  El [paquete `jq` de PyPI](https://pypi.org/project/jq/) serializa tus datos
  a texto JSON y los vuelve a convertir en cada llamada; purejq evalúa directamente sobre objetos de Python.
- **En archivos grandes, la CLI supera al binario C de jq de extremo a extremo.** La ejecución en archivos grandes
  está dominada por el análisis JSON, y el analizador respaldado por C de CPython es más rápido que el de jq.
- **Es jq real**: 751/781 casos (96.2%) de la propia suite de pruebas de jq pasan —
  la suite está incluida en este repositorio y se ejecuta en CI en cada commit.

Donde jq en C aún gana: rendimiento bruto de filtros en flujos ya analizados en
pipelines de shell. Si puedes instalar binarios y ese es tu caso de uso, usa jq.

## Benchmarks

Medido con [tools/bench.py](tools/bench.py): MacBook de serie M, CPython
3.13, jq 1.8.1 (arm64 nativo), **mediana de 7 ejecuciones**, y la salida de cada carga de trabajo
verificada como idéntica byte a byte contra el binario de jq primero. Reproduce ambos:
`python3 tools/bench.py 1000000 --verify`.

**Integrado en Python** — array de 100k objetos, ya analizado, en proceso:

| carga de trabajo | purejq | `jq` PyPI (vinculaciones C) |
|---|---:|---:|
| flujo de acceso a campos | 9 ms | 368 ms |
| filtrado + conteo | 55 ms | 442 ms |
| map + agregación | 18 ms | 444 ms |
| group_by | 112 ms | 704 ms |
| transformación + ordenación | 136 ms | 899 ms |
| filtro de expresiones regulares | 127 ms | 747 ms |

*Los números de las vinculaciones son su mejor caso (entrada de texto JSON); pasar objetos de Python,
su modo habitual, es un ~10% más lento.*

**Línea de comandos, de extremo a extremo** — archivo de 93 MB (1M objetos), análisis + filtrado + salida:

| carga de trabajo | purejq | jq 1.8 (binario C) |
|---|---:|---:|
| búsqueda única | 0.51 s | 1.68 s |
| filtrado + conteo | 1.08 s | 1.96 s |
| group_by | 2.32 s | 3.89 s |

*CLI de purejq medida con el extra opcional [orjson](https://github.com/ijl/orjson)
(`pip install 'purejq[speed]'`); con stdlib json solo es un ~25–35%
más lento y aún así va por delante en estas cargas de trabajo.*

**Cargar JSON grande en Python**: el archivo de 93 MB se analiza en 0.73 s con
stdlib json (128 MB/s) o 0.43 s con orjson (219 MB/s) — la carga de entrada es
a velocidad de C en cualquier caso y escala linealmente.

**¿Más grande que la memoria? Úsalo en streaming.** `--stream` analiza la entrada incrementalmente
y emite eventos `[path, leaf]` de jq, por lo que puedes procesar un archivo mucho más grande
que la RAM con una memoria aproximadamente constante:

```sh
# reconstruye y filtra cada registro de un array enorme, en memoria constante
purejq -n --stream 'fromstream(1|truncate_stream(inputs)) | select(.v > 0)' huge.json
```

En un array de 56 MB con 2M registros, reconstruir cada elemento alcanza un pico de **22 MB
RSS en streaming vs 612 MB al cargar todo el array** — 28x menos, misma salida.

**PyPy** (100k objetos, mismo código, sin cambios): filtrado + conteo 13 ms,
map + agregación 2 ms, group_by 33 ms, transformación + ordenación 70 ms — aproximadamente
de 2 a 9 veces más rápido que CPython para cargas de trabajo intensas.

Cómo es tan rápido, en una línea: los programas se compilan una vez en cierres de Python con
vinculación estática y caminos rápidos de salida única — la evaluación nunca recorre el
AST de nuevo, y las formas comunes omiten por completo la maquinaria de generadores.

## Compatibilidad con jq

751/781 de la suite de pruebas oficial de jq. Cada diferencia restante se lista en
[expected_failures.txt](tests/conformance/expected_failures.txt); se dividen
en tres categorías:

- el **sistema de módulos** (`import`/`include`) aún no está implementado
- los **enteros son exactos** (precisión arbitraria, como gojq) en lugar de redondear
  a doubles — es deliberado
- algunas **redacciones de mensajes de error** difieren

Todo lo demás está: rutas y todos los operadores de asignación,
`reduce`/`foreach`, `try`/`catch`, `label`/`break`, desestructuración `?//`,
interpolación de cadenas, `@formats`, funciones integradas de expresiones regulares, streaming
(`tostream`/`fromstream`), fechas y las adiciones de jq 1.8.

Banderas de la CLI: `-n -r -R -j -c -s -S -a -e -f --stream --indent --tab --arg
--argjson`. Las salidas son iteradores perezosos — `purejq.compile("repeat(. * 2)").run(1)` produce
indefinidamente.

## Compatibilidad

CPython 3.9–3.14 y PyPy, cero dependencias en tiempo de ejecución, garantizado por
[CI](.github/workflows/ci.yml) en cada push.

## Contribución y detalles internos

Consulta [CONTRIBUTING.md](CONTRIBUTING.md) — la suite de conformidad es el
marcador, `tools/bench.py` es el velocímetro.

## Licencia

[MIT](LICENSE)
