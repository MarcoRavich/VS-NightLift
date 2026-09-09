Ho preparato una nuova implementazione del bilanciamento del bianco basata su **stime robuste e filtraggio temporale dei guadagni**, senza uno stato EMA dipendente dall’ordine di elaborazione dei fotogrammi.

**[Scarica il pacchetto completo: AutoWhite temporale ottimizzato](sandbox:/mnt/data/AutoWhite_temporal_ottimizzato.zip)**

| File principale                                                                       | Contenuto                                                                     |
| ------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| [autowhite_temporal.py](sandbox:/mnt/data/awb_temporal_package/autowhite_temporal.py) | Modulo autonomo consigliato, senza dipendenze da `helpers`, `misc` o `vszip`. |
| [color_optimized.py](sandbox:/mnt/data/awb_temporal_package/color_optimized.py)       | Versione completa del tuo file, con la sola sezione AutoWhite sostituita.     |
| [README_IT.md](sandbox:/mnt/data/awb_temporal_package/README_IT.md)                   | Installazione, esempi, parametri, architettura e limiti.                      |
| [ANALISI_IT.md](sandbox:/mnt/data/awb_temporal_package/ANALISI_IT.md)                 | Analisi dettagliata del sorgente, comprese le criticità delle altre funzioni. |

**Stato della verifica:** sono passati **71 test numerici**. Nel pacchetto sono presenti anche **11 test d’integrazione VapourSynth**, ma non ho potuto eseguirli perché il runtime nativo non è disponibile in questo ambiente. Non considero quindi verificati il rendering effettivo, la compatibilità della tua specifica installazione o le prestazioni in FPS.

## 1. Diagnosi dello script originale

### AutoWhite non contiene alcuna stabilizzazione temporale

`AutoWhiteAdjust` ricava i tre guadagni esclusivamente dalle medie RGB del fotogramma corrente. `AutoWhite` e `AutoWhiteZ` forniscono al callback soltanto statistiche dello stesso indice: il passaggio a `vszip` cambia il calcolo delle medie, **non introduce coerenza temporale**.  

La conseguenza del modello è importante: quando entra nell’inquadratura un oggetto colorato, cambiano le medie dei canali e quindi i guadagni, anche se l’illuminazione è rimasta costante. Il filtro può così trasformare un cambiamento del contenuto in una variazione del bilanciamento del bianco.

L’AutoWhite originale è comunque **senza stato**: il suo problema principale è la variabilità della stima, non il dizionario persistente che compare invece in `AutoGain`.

### Manca la verifica che i piani siano effettivamente RGB

Le funzioni trattano i piani `0`, `1` e `2` come rosso, verde e blu, senza verificare la famiglia colore né eseguire una conversione. Passando un clip YUV, verrebbero interpretati come RGB i piani Y, U e V. Inoltre, non viene stabilito se i campioni RGB siano lineari oppure codificati con una curva di trasferimento.  

Questo non rende errato ogni impiego della funzione: un chiamante che prepara correttamente l’RGB evita il primo problema. Resta però un requisito implicito e non controllato.

Nella nuova versione la conversione è esplicita: **RGB float32 lineare per il lavoro interno**, poi ritorno al formato iniziale. Matrice, transfer e range sono risolti dai metadati o dagli argomenti forniti, senza dedurre arbitrariamente BT.709 dalla risoluzione. La documentazione di `resize` conferma sia il supporto alle conversioni sia la necessità di specificare i parametri mancanti. ([VapourSynth][1])

### La normalizzazione originale non conserva la luminanza

Nel codice, i rapporti di correzione vengono normalizzati mediante la loro media quadratica, cioè RMS. Non è un vincolo sulla luminanza del contenuto. 

Per esempio, con medie:

$$
(R,G,B)=(0{,}5,\ 0{,}25,\ 0{,}125)
$$

i guadagni diventano:

$$
(g_R,g_G,g_B)=\frac{(1,2,4)}{\sqrt{7}}
$$

e le tre medie corrette risultano tutte circa \(0{,}188982\). L’immagine viene neutralizzata secondo l’ipotesi gray-world, ma il livello risultante deriva dalla normalizzazione RMS.

Una precisazione: **i guadagni originali non sono illimitati**. Per medie positive normalizzate, senza intervento dell’epsilon, la loro RMS è uno e ciascuno è al massimo \(\sqrt{3}\), circa 1,732. Il problema è l’assenza di un limite esplicito configurabile e di una normalizzazione legata alla luminanza stimata, non una crescita arbitraria dei guadagni.

### Le medie globali non distinguono campioni utili e contenuto dominante

Non sono presenti maschere di validità, soglie per ombre e alte luci, regioni d’analisi, valutazione del supporto o riduzione degli outlier. Tutti i campioni contribuiscono alle medie.  

Anche qui occorre evitare una diagnosi troppo generica: **bande perfettamente nere, da sole, non alterano necessariamente i rapporti fra le medie RGB**. Nel modello ideale le scalano tutte nello stesso modo. Bordi colorati, loghi, sottotitoli, clipping e oggetti cromaticamente dominanti possono invece modificare la stima.

## 2. Perché non ho riutilizzato l’EMA di AutoGain

Il dizionario persistente di `AutoGain` conserva `scale` e `offset` e li aggiorna a ogni callback, senza verificare che lo stato provenga dal fotogramma `n-1`. Una richiesta in ordine diverso, un seek o una riesecuzione possono quindi utilizzare lo stato lasciato da un altro indice. 

VapourSynth dispone di elaborazione concorrente e richieste asincrone dei frame: la cronologia delle chiamate non è una base appropriata per definire implicitamente una ricorrenza temporale. Un lock proteggerebbe l’accesso al dizionario, ma **non renderebbe cronologico l’ordine degli aggiornamenti**. ([VapourSynth][2])

Ho inoltre individuato problemi separati in quelle funzioni:

| Problema                                                    | Conseguenza                                                                                                    |
| ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `CopyFrameProps(sc, stats)` senza selezione delle proprietà | Sostituisce le proprietà di `sc`, invece di unirle: i flag appena prodotti da `SCDetect` possono andare persi. |
| `ema_alpha / max(gain_limit, 0.01)`                         | Può superare uno: per esempio, `0.15 / 0.05 = 3`, quindi non è più una media convessa.                         |
| Dispatch `AutoGainZ(clip)`                                  | Non inoltra gli argomenti ricevuti da `AutoGain`.                                                              |
| Espressione `(x + offset) * scale`                          | Non coincide con `x * scale + offset`, mentre l’offset target viene già calcolato come `-scale * min`.         |

Questi punti derivano direttamente dalle chiamate e dalle formule del sorgente; il comportamento sostitutivo di `CopyFrameProps` è confermato dalla documentazione ufficiale.   ([VapourSynth][3])

**Le funzioni AutoGain non sono state modificate nella consegna.** Se restano nella catena originale, possono continuare a introdurre instabilità indipendenti dal nuovo AutoWhite.

## 3. Architettura della nuova versione

### Stima su miniatura con una maschera comune ai tre canali

Il filtro costruisce un ramo d’analisi a risoluzione ridotta. La maschera scarta campioni troppo scuri, prossimi alla saturazione di livello, molto cromatici o non finiti; applica inoltre un taglio delle code di luminanza e una preferenza morbida per i campioni meno cromatici.

**R, G e B utilizzano esattamente gli stessi pesi spaziali.** Questo evita di confrontare medie ottenute da popolazioni differenti, come accadrebbe selezionando indipendentemente i campioni di ciascun canale.

Il parametro `crop` permette di escludere dall’analisi bordi o sovraimpressioni note, senza ritagliare il video in uscita.

La “confidenza” implementata misura il **supporto utilizzabile della stima**: non è una probabilità calibrata che l’illuminante individuato sia corretto.

### Guadagni normalizzati e limitati in dominio logaritmico

Date le medie pesate \(\mu_c\), il target neutro è:

$$
T=\sum_c a_c\mu_c
$$

dove \(a_c\) sono i coefficienti di luminanza. I guadagni istantanei sono:

$$
g_c=\frac{T}{\max(\mu_c,\varepsilon)}
\qquad
\ell_c=\log(g_c)
$$

Il vettore \(\ell\) viene ridimensionato nel suo complesso per rispettare `max_gain`, anziché troncare separatamente i canali.

Questa normalizzazione conserva la luminanza delle **medie stimate** nella correzione istantanea completa e non limitata. Non promette luminanza invariata per ogni pixel dopo smoothing, forza parziale e protezione delle alte luci.

### Filtro temporale deterministico, confinato alla scena

Per il fotogramma \(n\), la modalità predefinita considera:

$$
[n-r,\ n+r]
$$

ma interrompe la finestra ai primi confini di scena rilevati. Nella finestra applica pesi gaussiani, pesi di supporto e una riduzione robusta degli outlier rispetto a una mediana pesata.

La scelta del dominio logaritmico ha una motivazione semplice: guadagni reciproci vengono trattati simmetricamente. La media logaritmica di 0,5 e 2 corrisponde a un guadagno uno; la loro media aritmetica è invece 1,25.

Nel codice non ci sono un `previous_gain` persistente, una storia illimitata dei fotogrammi o chiamate ricorsive a `get_frame()` nei callback. **La correzione è una funzione degli indici e dei dati della finestra**, non dell’ultimo frame richiesto.

Quando manca evidenza nel frame corrente, possono contribuire i vicini validi della stessa scena. Se manca evidenza nell’intera finestra, il filtro torna all’identità: non trascina indefinitamente un vecchio bilanciamento.

### Gestione degli stacchi

La nuova implementazione combina le euristiche interne con `_SceneChangePrev`, `_SceneChangeNext` ed eventuali indici espliciti `cuts`. I due flag hanno precisamente il significato di inizio e fine scena descritto nell’API VapourSynth. ([VapourSynth][4])

Per usare esclusivamente confini già verificati:

```python
out = AutoWhiteTemporal(
    src,
    cuts=[240, 811, 1250],  # Primi frame delle nuove scene, indici da zero
    scene_threshold=None,
    scene_chroma_threshold=None,
)
```

Flash, dissolvenze e movimenti rapidi possono confondere le euristiche interne. L’isolamento fra scene è garantito **rispetto ai confini forniti o rilevati**, non rispetto a uno stacco che nessun detector abbia riconosciuto.

### Applicazione al solo frame corrente

I guadagni vengono applicati con filtri nativi. Con Akarin disponibile viene usato un `Expr` statico che legge i guadagni dalle proprietà; altrimenti si utilizza `FrameEval` con `std.Expr` e dipendenze dichiarate tramite `clip_src`. Akarin documenta l’accesso alle proprietà con sintassi `x.NomeProprieta`; `FrameEval` documenta l’impiego di `clip_src` per descrivere i clip richiamati dal callback. ([GitHub][5])

**Non vengono mediati pixel appartenenti a fotogrammi diversi.** Anche la protezione delle alte luci combina originale e corretto dello stesso fotogramma: non è una media temporale dell’immagine.

## 4. Utilizzo consigliato

Metti `autowhite_temporal.py` nella stessa cartella dello script `.vpy`, oppure in una directory presente in `sys.path`.

Questo esempio descrive **una sorgente SDR BT.709 limited**; i parametri cromatici vanno adattati al materiale reale:

```python
from autowhite_temporal import AutoWhiteTemporal

# src è il VideoNode restituito dal tuo decoder.
out = AutoWhiteTemporal(
    src,
    matrix_in="709",
    transfer_in="709",
    primaries_in="709",
    range_in="limited",

    radius=12,
    sigma=6.0,
    strength=0.8,
    analysis_width=320,
    max_gain=2.0,

    highlight_protect=0.90,
    debug=True,
)

out.set_output()
```

Con metadati completi e corretti, gli override cromatici possono essere omessi:

```python
out = AutoWhiteTemporal(src, radius=12, strength=0.8)
```

Gli argomenti `*_in` **descrivono i campioni già presenti**. Impostare `transfer_in="linear"` su un vero segnale gamma o PQ non lo linearizza correttamente: dichiara una codifica sbagliata. Nella nuova implementazione gli override sono resi effettivi sui metadati, perché `resize` attribuisce normalmente precedenza alle proprietà esistenti rispetto agli argomenti `*_in`. ([VapourSynth][1])

### Regolazione temporale

| Obiettivo                                   | Impostazione iniziale                   |
| ------------------------------------------- | --------------------------------------- |
| Compromesso generale a 24/25 fps            | `radius=12`, `sigma=6`, `strength=0.8`  |
| Maggiore stabilità, adattamento più lento   | Aumentare insieme `radius` e `sigma`    |
| Correzione più prudente                     | Ridurre `strength`, per esempio a `0.5` |
| Nessuna dipendenza futura aggiunta dall’AWB | `causal=True`                           |
| Esclusione di una fascia dall’analisi       | `crop=(sinistra, alto, destra, basso)`  |

Con `radius=12`, la modalità simmetrica utilizza al massimo 25 campioni e, a 24 fps, richiede fino a mezzo secondo di futuro. È un orizzonte matematico della finestra, **non una misura della latenza complessiva**.

La modalità causale usa soltanto il presente e il passato, ma risponde più lentamente ai cambiamenti reali. Non rende automaticamente a latenza zero il decoder o gli altri filtri.

## 5. Ottimizzazioni effettive e limiti prestazionali

L’ottimizzazione principale consiste nel separare **analisi, controllo temporale e applicazione**.

Per esempio, a 1920×1080 una miniatura 320×180 contiene 36 volte meno campioni. NumPy lavora sulla miniatura e sui piccoli vettori di controllo; i collegamenti temporali trasportano clip di metadati 1×1, non il ramo RGB corretto a piena risoluzione.

Akarin evita la creazione di una nuova espressione per ogni frame. Il fallback standard mantiene invece quel meccanismo, ma senza cambiare algoritmo e senza una cache Python illimitata.

Questo **non dimostra che l’intera pipeline sia più veloce dell’originale**: linearizzazione, conversione YUV/RGB, protezione delle alte luci e riconversione hanno un costo. L’AWB originale, molto più semplice e applicato direttamente ai campioni ricevuti, può essere meno costoso. Senza un benchmark sul tuo ambiente non sarebbe corretto dichiarare un incremento di FPS.

## 6. Risultati delle verifiche

I test numerici coprono neutralità, dominante uniforme, maschere comuni, limiti dei guadagni, assenza di campioni validi, isolamento fra scene, indicizzazione ai bordi, causalità e identità dei risultati con richieste sequenziali, inverse, casuali e concorrenti del nucleo matematico.

Nell’[esperimento riproducibile incluso](sandbox:/mnt/data/awb_temporal_package/numeric_report.json), su 240 vettori sintetici di log-guadagno:

| Misura                                                     |          Risultato |
| ---------------------------------------------------------- | -----------------: |
| RMS della variazione interframe prima dello smoothing      |         0,03510436 |
| RMS dopo lo smoothing                                      |         0,00074845 |
| Riduzione nella specifica prova sintetica                  |         **97,87%** |
| Risultati con ordine casuale rispetto a quello sequenziale | Esattamente uguali |

**Il 97,87% non è una misura del flicker eliminato su video reali**, né dell’accuratezza cromatica: verifica soltanto il comportamento del filtro temporale su quella sequenza sintetica.

I test nativi forniti verificano anche roundtrip YUV, proprietà, flag di scena, richieste asincrone e confronto Akarin/std, ma restano da eseguire nel tuo ambiente:

```bash
python -m pytest -q -rs
```

## 7. Criticità residue da considerare nella tua catena

Nel resto del file ho documentato, senza modificarle, altre anomalie: la condizione `limited` di `RGBAdjust` confrontata con `RANGE_FULL`; il riconoscimento del 4:2:0 come 4:2:2 in `HighBitDepthHistogram`; e la scalatura di `ClipRGB` costruita come per gli interi anche quando l’ingresso è float.   

Per la nuova AWB restano tre limiti sostanziali.

**Colorimetria:** la versione fornita rifiuta PQ/HLG diretti e materiale interlacciato. La pipeline HDR e la scala luminosa devono essere definite esplicitamente; il modulo non esegue tone mapping.

**Ambiguità del contenuto:** una parete colorata o un’illuminazione scenica intenzionale possono ancora essere interpretate come dominante. Maschere e smoothing rendono la stima più prudente, ma non trasformano l’ipotesi gray-world in una misura dell’illuminante reale.

**Formato di uscita:** viene conservato il formato, non l’identità dei campioni attraverso un roundtrip YUV/RGB. Il bypass `strength=0` restituisce invece esattamente il nodo originale. La protezione delle alte luci attenua la correzione, ma non è un gamut mapping e non garantisce assenza assoluta di clipping; inoltre, `Expr` limita automaticamente i risultati interi al loro intervallo rappresentabile, mentre i float non vengono limitati automaticamente. ([VapourSynth][6])

La modifica centrale è quindi **rendere il bilanciamento del bianco una funzione temporale riproducibile e confinata alla scena**, anziché una successione di correzioni indipendenti o un’EMA legata all’ordine dei callback. Il file originale è rimasto intatto; la versione completa modificata conserva inalterate le altre 18 funzioni.

[1]: https://vapoursynth.com/doc/functions/video/resize.html "https://vapoursynth.com/doc/functions/video/resize.html"
[2]: https://vapoursynth.com/doc/pythonreference.html "https://vapoursynth.com/doc/pythonreference.html"
[3]: https://vapoursynth.com/doc/functions/video/copyframeprops.html "https://vapoursynth.com/doc/functions/video/copyframeprops.html"
[4]: https://vapoursynth.com/doc/apireference.html "https://vapoursynth.com/doc/apireference.html"
[5]: https://github.com/AkarinVS/vapoursynth-plugin "https://github.com/AkarinVS/vapoursynth-plugin"
[6]: https://vapoursynth.com/doc/functions/video/expr.html "https://vapoursynth.com/doc/functions/video/expr.html"


---

**Sì: la logica di preanalisi che hai allegato è una buona base per determinare una sola terna di guadagni RGB e applicarla all’intero filmato.** Per evitare le oscillazioni introdotte dal ricalcolo del bianco a ogni fotogramma, questa soluzione è più diretta dello smoothing temporale: i coefficienti vengono calcolati prima dell’elaborazione e poi rimangono costanti.

Occorre però separare due obiettivi:

> **Una correzione costante garantisce la stabilità dei coefficienti, ma non garantisce che quella correzione sia adatta a tutte le scene.**

Nel tuo caso riutilizzerei quindi **l’architettura dell’analizzatore**, ma aggiungerei misure cromatiche e una verifica della compatibilità dei campioni con una correzione globale. Il codice attuale, da solo, non contiene le informazioni necessarie per ricavare quei guadagni.

## 1. Che cosa fa effettivamente l’analizzatore allegato

Il file è progettato come passaggio di analisi per StaxRip: apre la sorgente tramite BestSource, costruisce un ramo di analisi ridotto, richiede alcuni fotogrammi e salva un report JSON. Questa separazione fra analisi preliminare e successiva elaborazione è esattamente la parte da conservare.   

### Le statistiche attuali sono esclusivamente di luminosità

`build_analysis_clip()` converte il contenuto in GRAY16 e produce due proprietà:

| Proprietà   | Significato nel codice                                                 | Utilità per il bilanciamento del bianco                                                     |
| ----------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `MeanLuma`  | Media della componente di luminosità, riportata sulla scala 0–255.     | Può aiutare a scartare campioni troppo scuri, ma non indica una dominante cromatica.        |
| `DarkRatio` | Frazione dei campioni della miniatura inferiori alla soglia impostata. | Può contribuire alla valutazione del supporto, ma non permette di calcolare i guadagni RGB. |

Il ramo di analisi elimina quindi le componenti cromatiche prima di calcolare le statistiche. Da queste due misure non è possibile ricostruire il rapporto fra rosso, verde e blu. 

Per l’AWB aggiungerei un **secondo ramo RGB**, conservando le misure attuali per le decisioni di NightLift.

### `fixed_pipeline` non significa necessariamente “bilanciamento del bianco fisso”

Questo è il punto più importante del file. `suggest_pipeline()` inserisce sempre:

```python
{"operation": "gray_world"}
```

La scelta fra `fixed_pipeline` ed `enhance_auto_per_frame` non aggiunge a questa operazione alcun guadagno precalcolato. Il report descrive una sequenza di operazioni, ma **non contiene una terna RGB da mantenere costante**. 

Senza il codice di `enhancer.vpy` non si può stabilire come venga eseguita quell’operazione. Se il suo `gray_world` ricalcola le medie per ogni fotogramma, continuerà a produrre guadagni variabili anche quando la lista delle operazioni è fissa.

Inoltre, `varies` verifica soltanto se cambiano le categorie di oscurità dei campioni. **Non verifica la stabilità cromatica:** due scene possono appartenere alla stessa categoria di luminosità e richiedere correzioni del bianco differenti. 

## 2. Il campionamento attuale è adatto a questo nuovo scopo?

**Come ricognizione iniziale sì; come unica verifica per bloccare il bianco di un lungometraggio, lo renderei più rappresentativo.**

La formula impiegata è:

```python
n = max(5, min(12, round(duration / 400) + 5))
```

I campioni sono distribuiti uniformemente fra il 15% e l’85% degli indici dei fotogrammi. Quando il frame rate non è utilizzabile, il numero richiesto diventa otto. 

Applicando esattamente questa formula, per materiale a frame rate costante si ottiene:

| Durata     | Campioni richiesti | Distanza approssimativa fra campioni | Porzione esclusa a ciascuna estremità |
| ---------- | -----------------: | -----------------------------------: | ------------------------------------: |
| 10 minuti  |                  7 |                1 minuto e 10 secondi |                 1 minuto e 30 secondi |
| 30 minuti  |                  9 |                2 minuti e 38 secondi |                 4 minuti e 30 secondi |
| 60 minuti  |                 12 |                3 minuti e 49 secondi |                              9 minuti |
| 120 minuti |                 12 |                7 minuti e 38 secondi |                             18 minuti |

Questi sono calcoli derivati dalla formula, non misurazioni di un filmato.

Su un video di due ore, dodici fotogrammi potrebbero non rappresentare molte condizioni presenti nel materiale. Inoltre, escludere complessivamente il 30% del filmato non equivale necessariamente a escludere soltanto introduzione e titoli: lo script lo presume, ma non li riconosce.

### Come modificherei la selezione

Come **configurazione iniziale da validare**, non come soglia universalmente corretta, proporrei circa un campione al minuto, con un minimo di 24 e un budget iniziale massimo di 160, sempre limitato al numero effettivo di fotogrammi disponibili.

Distribuirei i campioni su quasi tutta la durata utile, escludendo intervalli noti oppure campioni riconosciuti come inutilizzabili. Aggiungerei poi altri campioni nelle porzioni dove le stime risultano discordanti.

Il criterio decisivo non dovrebbe essere soltanto:

> «Quanto dura il filmato?»

ma anche:

> «I campioni raccolti concordano? Le porzioni non ancora verificate potrebbero richiedere un’altra correzione?»

Per ridurre la sensibilità a un flash o a un singolo fotogramma anomalo, si potrebbero usare piccoli gruppi locali di campioni. Li tratterei però come **una sola osservazione temporale aggregata**, evitando che cinque fotogrammi quasi identici pesino come cinque scene indipendenti.

### Attenzione al frame rate variabile

Nel codice la selezione è uniforme negli **indici**, non necessariamente nel tempo reale; `time_seconds` viene ricavato dall’indice e dal frame rate dichiarato. 

Per una distribuzione realmente temporale su materiale VFR servono i timestamp. Un dettaglio utile: in BestSource il parametro `timecodes` indica la **scrittura** di un file di tempi, non la lettura di quel file come guida al campionamento; l’analizzatore allegato non utilizza quei tempi per scegliere gli indici. ([GitHub][1])

## 3. Come ricaverei una terna RGB globale

Quella che segue è la **proposta di estensione**, non una funzionalità già presente nell’allegato.

### A. Analizzare RGB lineare con colorimetria definita

Per ogni campione costruirei una miniatura RGB float in luce lineare, con matrice, transfer e range correttamente definiti.

La conversione non deve basarsi sull’assunzione che ogni sorgente sia BT.709 limited. Nel file attuale `SOURCE_RANGE` è impostato a `"limited"` e il ramo RGB→GRAY usa la matrice `"709"`; per il nuovo ramo cromatico farei dipendere la conversione dai metadati verificati o da override espliciti.  

VapourSynth permette queste conversioni attraverso `resize`; la documentazione chiarisce anche che i metadati del frame, quando specificati, prevalgono sui corrispondenti argomenti `*_in`. Questo comportamento va gestito, non ignorato. ([VapourSynth][2])

### B. Riutilizzare lo stimatore robusto già preparato

Nel modulo precedente `_estimate()` lavora già su una miniatura RGB lineare, utilizza pesi spaziali comuni ai tre canali e restituisce log-guadagni, copertura e supporto della stima. È il componente che riutilizzerei, separandolo dal filtro temporale.  

Per ogni campione conserverei sia la stima sia le informazioni che permettono di valutarla. Un fotogramma privo di campioni cromatici utilizzabili dovrebbe ricevere **peso zero** nell’aggregazione: non dovrebbe diventare un voto artificiale a favore dei guadagni `(1, 1, 1)`.

La distinzione è sostanziale: “non ho informazioni” non significa “il bianco è già corretto”.

### C. Aggregare i rapporti cromatici, senza confonderli con l’esposizione

Un’impostazione che adotterei per l’aggregazione globale consiste nel rappresentare ogni campione mediante:

$$
z_i =
\left(
\log\frac{\mu_{R,i}}{\mu_{G,i}},
\log\frac{\mu_{B,i}}{\mu_{G,i}}
\right)
$$

dove le \(\mu\) sono le medie RGB ottenute con la stessa maschera e gli stessi pesi.

Questa rappresentazione ha una proprietà utile: se tutte le componenti del campione vengono moltiplicate per lo stesso fattore di esposizione, i due rapporti non cambiano.

Calcolerei quindi un centro robusto \(z^\*\), per esempio una mediana geometrica pesata. Da questo ricaverei i guadagni relativi:

$$
\widetilde g =
\left(
e^{-z_R^\*},\ 1,\ e^{-z_B^\*}
\right)
$$

La forza della correzione, i limiti ai guadagni e l’eventuale normalizzazione della luminosità andrebbero determinati **una volta sola**.

In particolare, non introdurrei successivamente una normalizzazione per fotogramma: trasformerebbe di nuovo una correzione globale in una trasformazione variabile.

Non farei invece una semplice media di tutti i pixel di tutti i campioni: così le porzioni più luminose o con maggiore supporto potrebbero dominare il risultato. Definirei esplicitamente il peso delle finestre temporali e, in caso di campionamento adattivo, eviterei di sovrappesare le regioni soltanto perché sono state analizzate più densamente.

## 4. La verifica fondamentale: i campioni ammettono una correzione comune?

**La parte più importante non è ottenere una mediana: è verificare che quella mediana rappresenti davvero il materiale.**

Immagina, come esempio, un filmato diviso fra scene con stime calde e scene con stime fredde. Una sintesi globale può risultare quasi neutra, pur non rappresentando bene nessuno dei due gruppi.

Per questo valuterei separatamente:

| Verifica proposta                  | Domanda a cui risponde                                                   |
| ---------------------------------- | ------------------------------------------------------------------------ |
| Supporto utilizzabile              | Ci sono abbastanza campioni informativi, distribuiti nel filmato?        |
| Dispersione cromatica              | Quanto differiscono le stime dal valore globale?                         |
| Presenza di gruppi distinti        | Esiste una sola popolazione di stime o più condizioni ricorrenti?        |
| Coerenza fra intervalli temporali  | Inizio, centro e fine suggeriscono correzioni compatibili?               |
| Campioni aggiuntivi di validazione | La correzione resta plausibile su fotogrammi non usati per determinarla? |

Misurerei la dispersione **prima di eliminare aggressivamente gli outlier**. Altrimenti una seconda condizione di illuminazione potrebbe essere rimossa come “anomalia”, facendo apparire uniforme un filmato che non lo è.

Anche un’ottima concordanza resta una verifica di coerenza, non una prova del bianco reale. Il metodo gray-world assume che la distribuzione dei colori della scena fornisca un riferimento mediamente neutro: quando questa assunzione non vale, la stima può essere sistematicamente sbagliata. È un limite del modello, non un difetto risolvibile soltanto aumentando il numero dei campioni. ([cs.sfu.ca][3])

Per la stessa ragione, nel modulo precedente la proprietà di confidenza misura il supporto disponibile, **non la probabilità che l’illuminante sia stato identificato correttamente**. 

### Quale decisione prendere dopo l’analisi

Adotterei questa politica:

| Esito della preanalisi                                          | Comportamento consigliato                                                                      |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Stime sufficientemente concordi e campionamento rappresentativo | Applicare una terna globale fissa.                                                             |
| Più gruppi cromatici persistenti                                | Segnalare che una terna unica è un compromesso; valutare correzzioni per scena o segmento.     |
| Evidenza insufficiente                                          | Non forzare una correzione automatica; mantenere l’identità o usare un riferimento verificato. |
| Variazioni progressive da correggere                            | Valutare una traiettoria temporale dei guadagni, anziché un valore unico.                      |

Non passerei automaticamente alla correzione per scena soltanto perché le stime divergono: la differenza potrebbe dipendere dal contenuto o da un’illuminazione intenzionale, non da un errore.

## 5. Che cosa migliora rispetto all’AutoWhite temporale

L’AutoWhite originale leggeva le medie RGB del fotogramma corrente e ricavava da esse la correzione. La preanalisi globale cambierebbe proprio questo principio: **durante il rendering non ci sarebbe più una nuova stima del bianco**.  

La struttura diventerebbe:

```text
PREANALISI
Sorgente → campioni RGB → stime robuste → verifica → guadagni nel JSON

RENDERING
Sorgente → conversione RGB lineare → guadagni fissi → formato di uscita
```

L’applicazione dei tre moltiplicatori può essere effettuata con un unico nodo `std.Expr`, che supporta espressioni distinte per ciascun piano. Non servirebbe un callback Python che selezioni nuovi coefficienti per ogni frame. ([VapourSynth][4])

Questo elimina dal ramo di rendering la necessità di stimare e filtrare temporalmente i guadagni. Rimangono però il costo della preanalisi e quello delle conversioni cromatiche: **non dedurrei un incremento preciso degli FPS senza una misura sulla tua catena**.

Il meccanismo `get_frame_async()` del tuo analizzatore è adatto a un passaggio preliminare: restituisce future associati ai frame richiesti, che puoi raccogliere prima di salvare il report. Con un numero maggiore di campioni limiterei le richieste contemporaneamente pendenti e manterrei un ordine fisso di aggregazione.  ([VapourSynth][5])

### Che cosa non risolve una terna fissa

Una moltiplicazione costante non annulla oscillazioni del bianco **già presenti nella sorgente**.

Per esempio:

$$
\log\frac{R'_n}{G'_n}
=
\log\frac{R_n}{G_n}
+
\log\frac{g_R}{g_G}
$$

Il secondo termine è costante. Di conseguenza, prima di clipping e altre trasformazioni non lineari, le differenze temporali del rapporto cromatico originale rimangono.

La distinzione pratica è questa: **il metodo globale evita il “pompaggio” prodotto dall’AWB, ma non è di per sé uno stabilizzatore degli errori dell’AWB della videocamera.**

## 6. Come lo integrerei nel tuo flusso StaxRip

Manterrei il loader e la struttura del report, aggiungendo una sezione `white_balance` separata dalle decisioni sulla luminosità.

Questa sezione dovrebbe contenere i guadagni finali, il dominio cromatico in cui applicarli, la forza già incorporata nei coefficienti, gli indici analizzati, il supporto disponibile, le misure di dispersione e l’esito della verifica globale. Registrerei anche i parametri di analisi e un’identificazione della sorgente, per non riutilizzare accidentalmente un report relativo a un altro video o a un’altra configurazione.

Nel successivo enhancer, la modalità globale dovrebbe **sostituire** l’operazione `gray_world` dinamica, non precederla: altrimenti verrebbe applicata una seconda correzione che potrebbe reintrodurre la variabilità.

Il modulo `AutoWhiteTemporal` già consegnato non espone attualmente un parametro per caricare una terna globale: la sua interfaccia riguarda lo stimatore e la finestra temporale. Questa estensione richiederebbe quindi di separare esplicitamente la fase di stima dalla fase di applicazione; non basta aumentare `radius`. 

**In conclusione, adotterei la tua idea come modalità “AWB globale con preanalisi e blocco dei guadagni”.** È coerente con l’obiettivo di massima stabilità dei coefficienti. Conservarei però la modalità temporale come alternativa distinta, da usare quando l’obiettivo è correggere variazioni reali nel tempo.

Dall’analizzatore allegato si può definire questa architettura, ma non stabilire se un particolare filmato ammetta una correzione unica: per quello servono campioni RGB o un report cromatico. **Il passaggio decisivo è trasformare la preanalisi da classificatore della luminosità in stimatore del bianco accompagnato da una verifica della sua rappresentatività.**

[1]: https://github.com/vapoursynth/bestsource "GitHub - vapoursynth/bestsource: A super great audio/video source and FFmpeg wrapper · GitHub"
[2]: https://vapoursynth.com/doc/functions/video/resize.html?utm_source=chatgpt.com "Resize — VapourSynth R76 documentation"
[3]: https://www.cs.sfu.ca/~colour/research/colour-constancy.html?utm_source=chatgpt.com "Computational Vision Lab"
[4]: https://vapoursynth.com/doc/functions/video/expr.html "Expr — VapourSynth R76 documentation"
[5]: https://vapoursynth.com/doc/pythonreference.html "Python Reference — VapourSynth R76 documentation"


---

Ho creato uno **script autonomo di preanalisi per StaxRip/VapourSynth** che produce un report JSON con una terna di guadagni RGB globali, le statistiche dei campioni e una valutazione della loro concordanza.

**[Scarica il pacchetto completo — Preanalisi AWB globale](sandbox:/mnt/data/Preanalisi_AWB_globale_StaxRip.zip)**

| File                                                                                          | Utilizzo                                                                         |
| --------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| **[analysis_wb_global.vpy](sandbox:/mnt/data/awb_global_preanalysis/analysis_wb_global.vpy)** | Script principale, autonomo, da eseguire nel passaggio di preanalisi StaxRip.    |
| [wb_global.py](sandbox:/mnt/data/awb_global_preanalysis/wb_global.py)                         | Motore riutilizzabile e funzione per applicare successivamente i guadagni fissi. |
| [README_IT.md](sandbox:/mnt/data/awb_global_preanalysis/README_IT.md)                         | Configurazione, funzionamento, schema del report e integrazione.                 |
| [TESTING_IT.md](sandbox:/mnt/data/awb_global_preanalysis/TESTING_IT.md)                       | Verifiche eseguite e limiti della validazione.                                   |

**Verifica effettuata:** 103 test automatici superati. Sono inclusi anche **otto test d’integrazione nativa VapourSynth**, non eseguiti perché questo ambiente non dispone del runtime. Non sono quindi verificati il rendering sulla tua installazione, le prestazioni effettive o l’accuratezza cromatica su un tuo filmato.

## 1. Che cosa cambia rispetto al filtro originale

L’AutoWhite del tuo `color.py` calcola le medie RGB dei fotogrammi e le passa a `FrameEval` per ricavare la correzione. Il nuovo script sposta invece la stima in un **passaggio preliminare separato dal rendering**. 

Il flusso previsto è:

```text
PREANALISI
Video → campioni RGB lineari → stima globale → verifica → report JSON

RENDERING
Video → applicazione della stessa terna RGB → formato di uscita
```

Ho mantenuto **esattamente la chiamata di caricamento BestSource del tuo analizzatore**, senza modificare i file originali. Lo script principale contiene già tutte le funzioni necessarie: non richiede il precedente pacchetto temporale, `helpers`, `misc`, Akarin o vszip.

Servono **NumPy, VapourSynth e BestSource**, disponibili nello stesso ambiente Python utilizzato da StaxRip.

## 2. Campionamento più rappresentativo, con costo controllato

La configurazione iniziale è questa:

| Parametro                           | Valore predefinito | Effetto                                         |
| ----------------------------------- | -----------------: | ----------------------------------------------- |
| `interval_seconds`                  |             `60.0` | Circa un campione di stima al minuto.           |
| `min_samples` / `max_samples`       |       `24` / `160` | Limiti del gruppo usato per stimare i guadagni. |
| `min_validation` / `max_validation` |         `8` / `40` | Campioni aggiuntivi riservati alla verifica.    |
| `analysis_width`                    |              `320` | Larghezza massima della miniatura.              |
| `analysis_max_height`               |              `320` | Limite anche per i video verticali.             |
| `max_inflight`                      |                `2` | Richieste contemporaneamente pendenti.          |
| `sample_window`                     |       `(0.0, 1.0)` | Campionamento sull’intero filmato.              |

Per un filmato nominale di **due ore a 24 fps**, il piano predefinito produce **120 campioni di stima e 30 di verifica**, oltre alla lettura del fotogramma zero per i metadati. Questo conteggio è verificato dai test inclusi.

La selezione è distribuita in intervalli uniformi, con piccoli spostamenti pseudocasuali riproducibili. Le richieste vengono ordinate per indice e i risultati aggregati in ordine fisso. I campioni di verifica **non contribuiscono a calcolare il candidato**.

Il limite alle richieste usa `get_frame_async()`, che nell’API VapourSynth restituisce un oggetto *future* associato al fotogramma richiesto. Il codice raccoglie e chiude esplicitamente i frame, anche in caso di errore. ([VapourSynth][1])

**Non vengono analizzati tutti i fotogrammi**, ma non va confuso questo con il costo totale della lettura: indicizzazione, seek e decodifica possono richiedere lavoro aggiuntivo. Inoltre, il loader originale conserva `cachesize=1000`; il limite di due richieste non impone un limite equivalente alla memoria complessiva del decoder. BestSource distingue infatti cache interna e risorse delle istanze di decodifica. ([GitHub][2])

## 3. Come vengono stimati e verificati i guadagni

Il ramo d’analisi converte i campioni in **RGB float32 lineare prima del ridimensionamento**. La gestione di matrice, transfer e range è esplicita: non viene dedotto automaticamente BT.709 dalla risoluzione. Queste conversioni sono realizzate tramite le funzioni native `resize`. ([VapourSynth][3])

Sulle miniature, l’implementazione applica una maschera comune ai tre canali, escludendo campioni non finiti, troppo scuri, quasi saturi di livello o eccessivamente cromatici. Una griglia **8×6** permette poi di aggregare le stime delle diverse zone senza assegnare automaticamente maggiore peso alle zone più luminose.

La stima viene rappresentata mediante:

$$
\left(\log\frac{R}{G},\ \log\frac{B}{G}\right)
$$

e aggregata con una **mediana geometrica pesata**. La forza della correzione e la normalizzazione luminosa vengono incorporate una volta sola nei coefficienti esportati.

Prima di raccomandare quella terna, il programma controlla la dispersione dei campioni, la concordanza del gruppo di verifica, la coerenza fra quattro porzioni temporali e l’eventuale presenza di gruppi cromatici distinti.

Le soglie sono **criteri euristici configurabili**, non livelli di confidenza statistica calibrati. Una scena monocromatica può comunque produrre una stima concorde ma inappropriata: è un limite dell’ipotesi gray-world, che usa la distribuzione dei colori dell’immagine come riferimento per l’illuminante. ([Scuola di Scienze Informatiche][4])

### Il report distingue stima e raccomandazione

| Campo                                | Significato                                                                                       |
| ------------------------------------ | ------------------------------------------------------------------------------------------------- |
| `candidate_gains_rgb`                | Terna stimata, mantenuta disponibile anche quando la verifica non passa.                          |
| `recommended_gains_rgb`              | Terna raccomandata per l’uso automatico; diventa `[1, 1, 1]` quando l’evidenza non è sufficiente. |
| `usable_for_automatic_application`   | Esito della politica di accettazione.                                                             |
| `status`                             | `accepted`, `review_required` oppure `insufficient_data`.                                         |
| `reasons`                            | Motivi della mancata accettazione.                                                                |
| `fit_metrics` / `validation_metrics` | Supporto e dispersione dei due gruppi.                                                            |
| `segments`                           | Concordanza delle porzioni temporali.                                                             |

**L’identità non viene presentata come una stima del bianco corretto:** nei casi inconcludenti è semplicemente la scelta prudente di non intervenire automaticamente.

## 4. Configurazione in StaxRip

Usa `analysis_wb_global.vpy` nello stesso tipo di **passaggio Python di preanalisi** in cui eseguivi il tuo `analysis.vpy`.

Il report viene scritto in:

```text
%temp_file%_wb_analysis.json
```

È separato dal precedente report sulla luminosità, così non ne altera lo schema o le decisioni NightLift.

Nella prima sezione del file trovi `PARAMETERS` e `COLOR`. Con metadati completi e corretti, lascia gli override cromatici a `None`.

Per una sorgente **verificata come YUV SDR BT.709 limited**, la configurazione è:

```python
COLOR = dict(
    matrix_in="709",
    transfer_in="709",
    primaries_in="709",
    range_in="limited",
    chromaloc_in=None,
)
```

Questi valori **descrivono i campioni in ingresso**, non lo spazio colore desiderato in uscita. Il codice rende effettivi gli override sulle proprietà, perché `resize` normalmente dà precedenza ai metadati già presenti rispetto agli argomenti `*_in`. ([VapourSynth][3])

È gestita anche la differenza fra `_Range` e `_ColorRange`: i due identificatori usano codifiche numeriche opposte per full e limited. ([VapourSynth][5])

Per escludere una fascia dall’analisi senza ritagliare il video:

```python
PARAMETERS = dict(
    analysis_width=320,
    interval_seconds=60.0,
    min_samples=24,
    max_samples=160,
    max_inflight=2,
    sample_window=(0.0, 1.0),
    crop=(0, 0, 0, 80),  # Sinistra, alto, destra, basso.
    strength=0.8,
    max_gain=1.8,
)
```

Il valore di `crop` è soltanto un esempio e va adattato al contenuto.

### Materiale a frame rate variabile

Quando è disponibile un file **timecode v2 corrispondente al clip**, lo script lo legge e campiona in base al tempo. Senza timestamp verificati, dichiara nel report l’uso del frame rate nominale o degli indici, senza presentare una durata VFR come precisa.

Il parametro BestSource `timecodes` **scrive** il file dei tempi: non lo legge. Nel wrapper ho conservato il comportamento del loader originale; il nuovo analizzatore legge il file dopo l’apertura della sorgente. ([GitHub][2])

## 5. Applicazione nel successivo passaggio di rendering

Nel pacchetto è inclusa una funzione che legge il report, controlla la corrispondenza della sorgente e applica i coefficienti nel dominio corretto.

Dopo aver reso importabile `wb_global.py`:

```python
from wb_global import apply_global_white_balance

clip = apply_global_white_balance(
    clip,
    r"%temp_file%_wb_analysis.json",
    source_path=r"%source_file%",
    pipeline_id="source",
    allow_unreliable=False,
)
```

Questo esempio con macro va usato dove StaxRip le sostituisce. Il nodo `clip` deve corrispondere allo **stesso punto della pipeline analizzato**.

Con un report accettato, il filtro applica tre moltiplicatori costanti mediante `std.Expr`, poi ripristina il formato iniziale. `Expr` supporta espressioni distinte per i piani del clip. ([VapourSynth][6])

Con un report inconcludente, il comportamento predefinito restituisce **esattamente il nodo originale**, senza conversioni. Un report incompleto o relativo a un’analisi fallita viene invece rifiutato.

**L’eventuale operazione `gray_world` dinamica dell’enhancer va sostituita, non eseguita dopo questa correzione.** Il nuovo JSON non modifica automaticamente il comportamento di un enhancer che non sia stato adattato a leggerlo.

## 6. Limiti da tenere presenti

Questa versione è destinata a **SDR progressivo RGB/YUV oppure RGB lineare esplicitamente normalizzato**. Rifiuta PQ/HLG diretti e materiale interlacciato; non esegue tone mapping, gamut mapping o correzioni separate per scena.

Il limite ai guadagni non garantisce l’assenza di clipping dei pixel. In particolare, i formati interi hanno un intervallo rappresentabile limitato, mentre l’elaborazione float può conservare valori esterni all’intervallo nominale. ([VapourSynth][6])

Infine, una terna fissa **evita variazioni dei coefficienti introdotte da questo AWB**, ma non annulla le oscillazioni cromatiche già registrate nella sorgente.

Il risultato operativo è quindi un prepass che non si limita a produrre tre numeri: **conserva il candidato, ne verifica la rappresentatività sui campioni e indica separatamente se la politica implementata ne raccomanda l’applicazione globale automatica.**

[1]: https://www.vapoursynth.com/doc/pythonreference.html "https://www.vapoursynth.com/doc/pythonreference.html"
[2]: https://github.com/vapoursynth/bestsource "https://github.com/vapoursynth/bestsource"
[3]: https://www.vapoursynth.com/doc/functions/video/resize.html "https://www.vapoursynth.com/doc/functions/video/resize.html"
[4]: https://www.cs.sfu.ca/~colour/research/colour-constancy.html "https://www.cs.sfu.ca/~colour/research/colour-constancy.html"
[5]: https://www.vapoursynth.com/doc/apireference.html "https://www.vapoursynth.com/doc/apireference.html"
[6]: https://www.vapoursynth.com/doc/functions/video/expr.html "https://www.vapoursynth.com/doc/functions/video/expr.html"


