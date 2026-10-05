from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langchain_ollama import OllamaLLM
from langchain_community.tools.tavily_search.tool import TavilySearchResults
from dotenv import load_dotenv
import os
from tavily import TavilyClient
import re
from datetime import datetime
import trafilatura

# Carica le variabili da .env
load_dotenv()

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
tavily = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

# Verifica se la chiave API è presente
if TAVILY_API_KEY is None:
    raise ValueError("La chiave API non è stata trovata nel file .env")


class State(TypedDict):
    search_results: list
    verified_search_results: list
    selected_articles: str
    final_post: str

search = TavilySearchResults()

# Nodo 1: Ricerca eventi su Tavily
def web_search_node(state):
    print("--- RICERCA EVENTI CON TAVILY---")
    query = "eventi musicali in Sicilia estate 2025"
    results = search.run(query)
    # Rimuove risultati con URL duplicati
    unique_urls = set()
    unique_results = []
    for result in results:
        if result['url'] not in unique_urls:
            unique_results.append(result)
            unique_urls.add(result['url'])
    results = unique_results

    state["search_results"] = results
    return state

# Nodo 2: Verifica se ci sono risultati
def check_search_results(state):
    print("--- VERIFICA RISULTATI DELLA RICERCA ---")
    search_results = state.get("search_results", [])
    if not search_results:
        print("Nessun risultato trovato.")
        return {"search_ok": False, **state}
    return {"search_ok": True, **state}


# Funzione di estrazione testo da URL
def estrai_testo_da_url(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded is None:
            return "Contenuto non disponibile."
        result = trafilatura.extract(downloaded, include_formatting=False, include_comments=False)
        return result if result else "Contenuto non disponibile."
    except Exception as e:
        print(f"Errore nell'estrazione del testo da {url}: {e}")
        return "Contenuto non disponibile."


# Nodo 3: verifica l'accuratezza
def verify_information(state):
    print("--- Verifica dell'accuratezza delle informazioni ---")
    ollama = OllamaLLM(model="mistral")
    
    # Estrai i risultati della ricerca
    search_results = state.get("search_results", [])
    
    verified_results = []
    # Chiedo a Ollama di verificare ogni evento/URL
    for result in search_results:
        MAX_CHARS = 3000
        testo_articolo = estrai_testo_da_url(result['url'])[:MAX_CHARS]
        prompt = f"""
        Ho trovato il seguente articolo: "{result['title']}" - URL: {result['url']}

        Contenuto estratto dall'articolo:
        {testo_articolo}

        Per favore, analizza l'accuratezza del contenuto sopra e rispondi solo nel seguente formato:

        Non fare eccezioni. Non indovinare o riempire i campi mancanti. Se un campo non è chiaramente indicato nel contenuto, segna "non specificato".

        Restituisci solo questi campi, nello stesso ordine e formattazione:

        - **Titolo**: {result['title']}
        - **Verificato**: Sì/No  
        - **Location**: (Città specifica o "Sicilia" o "non specificato")  
        - **Data**: (Data nel formato "gg mese aaaa" oppure "gg mese" oppure "non specificato")  
        - **Artista**: (Artisti noti e plausibili oppure "non specificato")  
        - **URL**: {result['url']}

        **Criteri per "Verificato: Sì"**:
        1. Deve esserci **data chiara** tra **giugno e settembre 2025** altrimenti impostalo come "non accurato". Se l'anno non è esplicitamente indicato, ma dal contesto si può dedurre chiaramente che si tratti dell’estate 2025, considera la data valida. 
        2. La location deve essere una **città specifica in Sicilia** o "Sicilia", altrimenti "non accurato". Se nell'articolo ci sono varie città allora elencale separandole con delle virgole.
        3. Gli artisti devono essere realmente esistenti e noti per concerti pubblici. Se il nome sembra inventato, incompleto o non corrisponde a un artista musicale conosciuto, scrivi "non accurato". Se nell'articolo ci sono vari artisti allora elencali separandoli con delle virgole.
        
        REGOLA FONDAMENTALE:
        Se **anche solo uno tra "Location", "Data" o "Artista" è "non accurato"**, allora imposta **obbligatoriamente** "Verificato: No".

        **IMPORTANTE**: Rispondi esattamente nel formato indicato sopra, senza alcuna spiegazione aggiuntiva o testo libero. Nessuna introduzione o commento. Solo i 6 campi richiesti, ben formattati.
        """       
        
        try:
            response = ollama.invoke(prompt)
        except Exception as e:
            print(f"Errore nella verifica con Ollama: {e}")
            response = "Verifica fallita"
            
        print("✅ Risposta di Ollama:")
        print(response)     
        
        response_clean = re.sub(r"[\*\_]", "", response.lower())  # Rimuove Markdown
        match_verificato = re.search(r"verificato\s*[:\-–—]?\s*(sì|no)", response_clean)

        if match_verificato:
            verificato = match_verificato.group(1).lower()
            if verificato in ["sì", "si"]:
                verificato = "sì"
            else:
                verificato = "no"
        else:
            verificato = "no"

        # Aggiungiamo il risultato verificato alla lista
        verified_results.append({
            "title": result["title"],
            "url": result["url"],
            "is_verified": verificato,
            "content": testo_articolo
        })
    
    # Aggiungiamo i risultati verificati allo stato
    state["verified_search_results"] = verified_results
    return state


# Nodo 4: Selezione miglior fonte tra quelle verificate
def select_best_sources(state):
    print("--- Selezione articolo di qualità e interesse ---")
    ollama = OllamaLLM(model="mistral")

    verified = [r for r in state.get("verified_search_results", []) if r["is_verified"] == "sì"]

    # Se non ci sono articoli verificati, fermiamo il processo
    if not verified:
        print("Nessun articolo verificato trovato. Fine del processo.")
        state["final_post"] = "Nessun articolo verificato trovato."
        return {"selected_ok": False, **state}

    # Costruisco lista di articoli 
    articoli = []
    unique_urls = set()  # Set per tenere traccia degli URL unici
    MAX_CONTENT = 2000

    for r in verified:
        url = r["url"]
        if url not in unique_urls:  # Aggiungi solo se l'URL non è già presente
            content = r["content"].replace('"', "'").strip()[:MAX_CONTENT]
            item = f'{{"title": "{r["title"]}", "url": "{r["url"]}", "content": "{content}"}}'
            articoli.append(item)
            unique_urls.add(url)
        else:
            print(f"URL duplicato ignorato: {url}")

    articoli = "\n\n".join(articoli)

    prompt = f"""
    Rispondi solo in italiano.
    Hai a disposizione una lista di articoli online già verificati come accurati, ciascuno relativo a eventi musicali in Sicilia previsti per l'estate 2025.
    Il tuo compito è selezionare solo quello che può essere considerato di **alta qualità** e di **maggiore interesse** per un pubblico ampio e informato.

    Valuta ogni risorsa secondo i seguenti criteri dettagliati:
    1. **Affidabilità della fonte**
    - La fonte è ufficiale, autorevole o conosciuta per fornire contenuti accurati? (es. quotidiani nazionali, testate locali affidabili, siti di eventi ufficiali)
    - Evita fonti dubbie o blog non riconosciuti, a meno che offrano un'informazione unica e ben strutturata.

    2. **Completezza e chiarezza delle informazioni**
    - L'articolo fornisce dettagli chiari come data, luogo e artisti coinvolti?
    - Le informazioni sono strutturate in modo comprensibile e facilmente leggibile?

    3. **Attualità**
    - La data di pubblicazione è recente? L'articolo è stato aggiornato recentemente?

    4. **Pertinenza e specificità**
    - L’articolo è centrato su eventi musicali e non su altri eventi culturali?
    - È focalizzato su concerti, festival o rassegne musicali specifiche?

    5. **Interesse per il pubblico**
    - Gli eventi coinvolgono artisti noti o emergenti che possono attrarre pubblico?
    - Include informazioni pratiche (es. biglietti, location, accessibilità, novità)?

    6. **Scopo dell'articolo**
    - L'articolo ha come scopo **informare**, **vendere**, o **influenzare**?
    - Se lo scopo è vendere o influenzare, l'articolo risulta comunque utile e imparziale? Se sì considera l'articolo di qualità

    7. **Contraddizioni interne o esagerazioni**
    - Esamina l'articolo per eventuali contraddizioni interne, come dichiarazioni che si smentiscono a vicenda.
    - Verifica se ci sono segnali di esagerazioni o affermazioni che sembrano eccessive, troppo promozionali, o inverosimili.
    - Se ci sono affermazioni non plausibili o incoerenti, segnalarle come segnali di bassa qualità.

    Articoli:
    {articoli}
    """
   
    try:
        response = ollama.invoke(prompt)
        print("\n✅ Articolo selezionato per qualità e interesse:")
        print(response)
        state["selected_articles"] = response
    except Exception as e:
        print(f"Errore nella selezione dell'articolo: {e}")
        state["selected_articles"] = "Errore nella selezione"
    return state



# Nodo 5 - Generazione del post
def generate_post_node(state, max_length_words=800):
    print("--- Generazione del post finale ---")
    ollama = OllamaLLM(model="mistral")

    articolo_selezionato = state.get("selected_articles", "")
    
    prompt = f"""
    Sulla base dell' articolo selezionato e valutato di alta qualità e interesse, scrivi un **articolo informativo per il web** con tono coinvolgente e professionale. Il testo è destinato a un pubblico ampio interessato alla musica, alla cultura e agli eventi estivi in Sicilia.

    **Lunghezza massima**: {max_length_words} parole  
    **Lingua**: italiano  
    **Stile**: informativo, chiaro, accessibile, ma ricco di dettagli e spunti culturali  
    **Obiettivo**: promuovere e raccontare gli eventi musicali più interessanti dell’estate 2025 in Sicilia.

    **Formato obbligatorio della risposta** (rispetta esattamente questa struttura):
    1. **Titolo**: breve, originale e accattivante
    2. **Introduzione**: contesto sull’estate musicale in Sicilia, importanza culturale e varietà degli eventi
    3. **Corpo del post**:
    - Crea un paragrafo separato per ogni evento principale (massimo 10 eventi)
    - Ogni paragrafo deve includere:
        - Una descrizione coinvolgente dell’evento, integrando naturalmente data, luogo e artisti principali, con attenzione all’atmosfera e al contesto culturale in cui si inserisce.
        - Una o più curiosità o particolarità: ad esempio, location suggestive, collaborazioni inedite, edizioni passate memorabili, o l’impatto dell’evento sulla comunità locale.
    4. **Sezione "Dettagli pratici"**:
    - Riepilogo sintetico con data, luogo e stato biglietti (disponibili / in vendita a breve)
    5. **Conclusione**:
    - Invito chiaro alla partecipazione
    - Riferimento alla bellezza della Sicilia e alla magia degli eventi estivi

    Utilizza un linguaggio semplice ma mai banale. Evita ripetizioni. Assicurati che ogni paragrafo abbia senso compiuto e che la struttura sia coerente.

    Articoli di riferimento:
    {articolo_selezionato}
    """

    try:
        response = ollama.invoke(prompt)
        print("✅ Articolo generato:")
        print(response)
        state["final_post"] = response
    except Exception as e:
        print(f"Errore nella generazione del post: {e}")
        state["final_post"] = "Errore durante la generazione del post."
    
    return state


# Creo il grafo
builder = StateGraph(State)

# Aggiungo i nodi e i loro collegamenti
builder.add_node("web_search", web_search_node)
builder.add_node("check_search_results", check_search_results)
builder.add_node("verify_information", verify_information)
builder.add_node("select_best_sources", select_best_sources)
builder.add_node("generate_post", generate_post_node)


builder.add_edge(START, "web_search")
builder.add_edge("web_search", "check_search_results")
builder.add_conditional_edges(
    "check_search_results",
    lambda state: state.get("search_ok"),  # Condizione: se la ricerca è OK
    {True: "verify_information", False: END}  # Se OK, va a "verify_information", altrimenti termina
)
builder.add_edge("verify_information", "select_best_sources")
builder.add_edge("select_best_sources", "generate_post")
builder.add_edge("generate_post", END)


# Compilo il grafo
graph = builder.compile()


with open("graph.md", "w") as f:
    f.write("```mermaid\n")
    f.write(graph.get_graph().draw_mermaid()) 
    f.write("\n```")

print("✅ Grafo scritto in graph.md")


# SCRITTURA ARTICOLO OGNI 3 GIORNI
INTERVALLO_GIORNI = 3
DATA_FILE = "ultima_esecuzione.txt"
OUTPUT_FILE = "ultimo_post.txt"

def giorni_passati_dall_ultima_esecuzione():
    if not os.path.exists(DATA_FILE):
        return INTERVALLO_GIORNI + 1
    with open(DATA_FILE, "r") as f:
        try:
            data = datetime.strptime(f.read().strip(), "%Y-%m-%d")
        except ValueError:
            return INTERVALLO_GIORNI + 1
    return (datetime.now() - data).days

def salva_data_odierna():
    with open(DATA_FILE, "w") as f:
        f.write(datetime.now().strftime("%Y-%m-%d"))

def salva_post_finale(post):
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(post)

def job():
    print("\nControllo se è il momento di generare un nuovo post...")
    if giorni_passati_dall_ultima_esecuzione() >= INTERVALLO_GIORNI:
        print("Avvio della generazione del post...")
        final_state = graph.invoke({})
        salva_post_finale(final_state.get("final_post", ""))
        salva_data_odierna()
        print(f"Articolo salvato in {OUTPUT_FILE}")
    else:
        print("Troppo presto per una nuova esecuzione.")


if __name__ == "__main__":
    job()

