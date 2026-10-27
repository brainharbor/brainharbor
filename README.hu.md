# brAInharbor

[English](README.md) · **Magyar**

Privát AI-asszisztens kis csapatoknak, a saját gépeden. Helyi nyelvi
modellekkel beszélgethetsz, és a saját dokumentumaidban kereshetsz úgy,
hogy semmi sem kerül ki felhőszolgáltatáshoz.

> Állapot: **v0.1.0**, az első kiadás. Az 1.0-ig még lehetnek nem
> visszafelé kompatibilis változások; frissítés előtt olvasd el a
> [változásnaplót](CHANGELOG.md).

## Mi van benne?

| Összetevő | Feladat |
| --- | --- |
| [Ollama](https://github.com/ollama/ollama) | Futtatja a nyelvi és a beágyazási (embedding) modelleket; ha van GPU, azon |
| [Open WebUI](https://github.com/open-webui/open-webui) | Csevegőfelület, felhasználói fiókok, dokumentumok feltöltése és keresés bennük (RAG) |
| [Caddy](https://caddyserver.com) | Fogadja a HTTPS-kapcsolatokat; csak ez nyit portot a gépen |
| `ollama/` | `brainharbor-hu`: a gemma4:26b a magyar rendszerprompttal és a tesztekben legjobbnak bizonyult beállításokkal |
| `open-webui/` | Az Open WebUI alapbeállításai: alapértelmezett modell, kikapcsolt gondolkodás, magyar rendszerprompt |
| `scripts/` | Előzetes ellenőrzés, modellek letöltése, mentés és visszaállítás |
| `tests/hu/` | Magyar nyelvi tesztek: minden modell ugyanazokat a kérdéseket kapja, a hibákat megszámoljuk |

A verziók a `.env.example`-ben rögzítettek, és csak egy tesztfuttatással
együtt változnak.

## Követelmények

- Linux, Docker Engine és a Docker Compose bővítmény
- 32 GB RAM ajánlott: az alapértelmezett modell 18 GB-os letöltés, és
  futás közben nagyjából 20 GB-ot foglal a VRAM-ban és a RAM-ban együtt
- Nem kötelező, de erősen ajánlott: NVIDIA GPU legalább 8 GB VRAM-mal,
  friss driverrel (RTX 50-es sorozathoz 570 vagy újabb) és az NVIDIA
  Container Toolkittel
- Szabad lemezterület: kb. 20 GB a konténerképeknek (a Docker mappájában,
  általában a `/` alatt) és kb. 50 GB a modelleknek

## Gyors kezdés

```sh
git clone https://github.com/brainharbor/brainharbor.git
cd brainharbor
cp .env.example .env
# a .env-ben állítsd be a DOMAIN és a WEBUI_SECRET_KEY értékét (openssl rand -hex 32)

scripts/preflight.sh            # ellenőrzi a gépet; a --gpu-test a GPU-t is kipróbálja
docker compose up -d
scripts/pull-models.sh          # letölti a modelleket, és elkészíti a brainharbor-hu-t
```

Nyisd meg a `https://<DOMAIN>` címet, és hozd létre az első fiókot: ez lesz
a rendszergazdai fiók. A később regisztrálók a rendszergazda jóváhagyására
várnak.

`CADDY_TLS=internal` esetén a böngészők addig figyelmeztetnek, amíg nem
bíznak meg a Caddy helyi gyökértanúsítványában. Így mentheted ki:

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./brainharbor-root.crt
```

Ezután a kliensgépeken importáld megbízható gyökértanúsítványként
(hitelesítésszolgáltatóként). A Firefox saját listát használ: Beállítások >
Adatvédelem és biztonság > Tanúsítványok > Tanúsítványok megtekintése >
Hitelesítésszolgáltatók > Importálás, és jelöld be, hogy webhelyek
azonosítására megbízható. Ne érd be azzal, hogy elfogadod a böngésző
figyelmeztetését: a Caddy 12 óránként megújítja a helyi tanúsítványait, és
a figyelmeztetés visszajön.

## Magyar alapbeállítások

Az új beszélgetések alapból a `brainharbor-hu` modellt használják: ez a
gemma4:26b magyar rendszerprompttal, 0,3-es hőmérséklettel és kikapcsolt
gondolkodással. Ez a beállítás hibázott a legkevesebbet a magyar
tesztjeinkben, a gondolkodás pedig csak lassította a válaszokat. Hibák így
is előfordulnak: amit kiküldesz, azt olvassa el egy ember.

- Az `ollama/brainharbor-hu.Modelfile` tartalmazza a modellt, a
  rendszerpromptot és a hőmérsékletet. A `scripts/pull-models.sh` építi
  meg belőle a modellt.
- Az `open-webui/defaults.env` ezt teszi alapértelmezett modellé,
  kikapcsolja a gondolkodást, és ugyanezt a rendszerpromptot állítja be az
  Open WebUI-ban. Az Open WebUI-t emellett a régi (legacy)
  függvényhívási módra kapcsolja: az alapértelmezett (natív) módban az
  Open WebUI beépített eszközeinek leírása kb. 6000 tokennel növeli minden
  üzenet hosszát, és a válasz másodpercekkel később indul.

## Tárolás

Minden adat a `compose.yaml` melletti `data/` mappában van: a modellek, az
Open WebUI adatbázisa és feltöltései, valamint a Caddy tanúsítványai. ZFS
esetén a [docs/zfs.md](docs/zfs.md) (angolul) leírja, milyen datasetekre
és beállításokkal érdemes szétosztani az adatokat, és hogyan költöztethető
át rájuk egy meglévő telepítés.

## Mentés

```sh
sudo scripts/backup.sh                 # egy archívum a backups/ mappába: adatok, tanúsítványok, .env
sudo scripts/restore.sh backups/brainharbor-ÉÉÉÉHHNN-ÓÓPPMM.tar.gz
```

Az archívumban a modelleken kívül minden benne van; a modelleket a
visszaállítás újra letölti. Éjszakai mentés, külső helyre másolás,
ZFS-pillanatképek és költözés új gépre: [docs/backup.md](docs/backup.md)
(angolul).

## Jó tudni

- Az első indulás után az Open WebUI a legtöbb beállítást a saját
  adatbázisában tárolja. Ha később módosítod a kapcsolódó `.env`-értékeket
  vagy az `open-webui/defaults.env` fájlt, annak nincs hatása; ilyenkor az
  adminpanelen állítsd át őket.
- Az Open WebUI-t változtatás nélkül, a saját arculatával használjuk, ahogy
  a [licence](https://docs.openwebui.com/license/) előírja.

## A magyar szövegek tesztelése

A kis helyi modellek hibáznak magyarul, és nagyon különböznek egymástól. A
`tests/hu/run.py` minden modellnek többször felteszi ugyanazokat a
kérdéseket, méri a sebességet, és megjelöli a helyesírási hibákat, a más
írásrendszerekből származó betűket és az ismert hibákat. Részletek:
[tests/hu/README.md](tests/hu/README.md) (angolul).

## Ami a v0.1-be bekerült

- [x] Compose stack GPU-támogatással és HTTPS-sel
- [x] Előzetes ellenőrző szkript
- [x] Magyar nyelvi tesztcsomag
- [x] ZFS-barát adatkiosztás és leírás
- [x] Mentő és visszaállító szkriptek, üres gépen kipróbálva
- [x] README magyarul és angolul
- [x] CI: shellcheck, a compose-fájlok ellenőrzése, a rendszerprompt egyezése

## Licenc

MIT, lásd [LICENSE](LICENSE). Az Ollamára, az Open WebUI-ra, a Caddyre és
a modellekre a saját licencük vonatkozik.
