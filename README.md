# Sports Chile — Scraper de Deportes

Genera un archivo HTML con todos los eventos deportivos del día para un espectador en Chile, con horarios en CLT y el canal donde verlos.

## Instalación

```bash
cd sports-chile
pip install -r requirements.txt
```

## Uso

```bash
# Hoy
python sports.py

# Fecha específica
python sports.py 2026-05-17
```

El script crea `deportes_YYYY-MM-DD.html` en el directorio actual y lo abre automáticamente en el navegador.

## Fuentes de datos

| Grupo | Deporte | Fuente |
|-------|---------|--------|
| 1 | Fútbol Europeo & Sudamericano | SofaScore API pública |
| 2 | NFL / NBA / MLB | ESPN API pública |
| 2 | UFC | ufc.com (scraping) |
| 3 | F1 | Ergast API |
| 3 | WEC / GT World Challenge | Sitios oficiales (scraping) |
| 4 | ATP Tennis | atptour.com (scraping) |

## Notas

- Si un grupo falla, se registra el error y el script continúa con los demás.
- Los horarios siempre se muestran en CLT (UTC-3), sin ajuste por horario de verano.
- Las señales (Dónde ver) son un mapa estático aproximado; pueden variar según el partido.
