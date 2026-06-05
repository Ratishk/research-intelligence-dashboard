"""Seed registry: 16 industries, each with the verified ≥10-source bootstrap set,
plus default ticker watchlists.

Every source here was validated through web research during planning (see the
plan file). The discovery engine grows each industry toward its target count and
cycles sources by measured signal yield. Entries with empty url/handle and a
note are deliberate slots the engine fills.

Each source tuple: (name, type, url, handle, tier)
  tier 1 = high-confidence anchor (poll daily); tier 2 = include with skepticism.
"""
from __future__ import annotations

# Reusable RSS/URL constants for sources cited across multiple industries.
INDUSTRIES: dict[str, dict] = {
    "AI / ML": {
        "group": "Tech",
        "sources": [
            ("Andrej Karpathy", "twitter", "https://x.com/karpathy", "karpathy", 1),
            ("GPU Mode", "youtube", "https://www.youtube.com/@GPUMODE", "@GPUMODE", 1),
            ("Yannic Kilcher", "youtube", "https://www.youtube.com/@YannicKilcher", "@YannicKilcher", 1),
            ("Latent Space", "rss", "https://www.latent.space/feed", "", 1),
            ("Simon Willison", "rss", "https://simonwillison.net/atom/everything/", "", 1),
            ("The Gradient", "rss", "https://thegradient.pub/rss/", "", 1),
            ("AI Explained", "youtube", "https://www.youtube.com/@aiexplained-official", "@aiexplained-official", 1),
            ("George Hotz", "twitter", "https://x.com/realGeorgeHotz", "realGeorgeHotz", 2),
            ("Two Minute Papers", "youtube", "https://www.youtube.com/@TwoMinutePapers", "@TwoMinutePapers", 1),
            ("Import AI (Jack Clark)", "rss", "https://importai.substack.com/feed", "", 1),
        ],
    },
    "Semiconductors / Tech Hardware": {
        "group": "Tech",
        "sources": [
            ("SemiAnalysis", "rss", "https://semianalysis.com/feed/", "dylan522p", 1),
            ("Asianometry", "youtube", "https://www.youtube.com/@Asianometry", "@Asianometry", 1),
            ("Fabricated Knowledge", "rss", "https://www.fabricatedknowledge.com/feed", "", 1),
            ("TechInsights", "rss", "https://www.techinsights.com/rss.xml", "", 1),
            ("Chips and Cheese", "rss", "https://chipsandcheese.com/feed/", "", 1),
            ("WikiChip", "twitter", "https://x.com/david_schor", "david_schor", 1),
            ("Ian Cutress / More Than Moore", "twitter", "https://x.com/IanCutress", "IanCutress", 1),
            ("Semiconductor Engineering", "rss", "https://semiengineering.com/feed/", "", 1),
            ("Anastasi in Tech", "youtube", "https://www.youtube.com/@AnastasiInTech", "@AnastasiInTech", 1),
            ("zephyr_z9", "twitter", "https://x.com/zephyr_z9", "zephyr_z9", 1),
            ("EETimes", "rss", "https://www.eetimes.com/feed/", "", 1),
            ("ASML Newsroom", "rss", "https://www.asml.com/en/news/press-releases/rss", "", 1),
        ],
    },
    "Software / SaaS / Internet & Media": {
        "group": "Tech",
        "sources": [
            ("Stratechery", "rss", "https://stratechery.com/feed/", "", 1),
            ("Clouded Judgement (Jamin Ball)", "rss", "https://cloudedjudgement.substack.com/feed", "jaminball", 1),
            ("Platformer", "rss", "https://www.platformer.news/rss/", "", 1),
            ("Matthew Ball", "rss", "https://www.matthewball.co/all?format=rss", "", 1),
            ("Simon Willison", "rss", "https://simonwillison.net/atom/everything/", "", 1),
            ("Modest Proposal", "twitter", "https://x.com/modestproposal1", "modestproposal1", 1),
            ("Spyglass (M.G. Siegler)", "rss", "https://spyglass.org/feed/", "", 1),
            ("The Information", "rss", "https://www.theinformation.com/feed", "", 1),
            ("MoffettNathanson", "rss", "https://www.moffettnathanson.com/feed/", "", 2),
            ("BG2 Pod", "youtube", "https://www.youtube.com/@bg2pod", "@bg2pod", 1),
        ],
    },
    "Macro / Rates / Financial Services": {
        "group": "Financials",
        "sources": [
            ("Lyn Alden", "rss", "https://www.lynalden.com/feed/", "LynAldenContact", 1),
            ("The Macro Compass (MacroAlf)", "rss", "https://themacrocompass.substack.com/feed", "MacroAlf", 1),
            ("Net Interest (Marc Rubinstein)", "rss", "https://www.netinterest.co/feed", "", 1),
            ("The Kobeissi Letter", "twitter", "https://x.com/KobeissiLetter", "KobeissiLetter", 1),
            ("The Short Bear", "twitter", "https://x.com/TheShortBear", "TheShortBear", 1),
            ("Citrini Research", "rss", "https://www.citriniresearch.com/feed", "", 1),
            ("Doomberg", "rss", "https://doomberg.substack.com/feed", "", 1),
            ("The Bear Cave", "rss", "https://thebearcave.substack.com/feed", "", 1),
            ("SEC EDGAR 8-K (key issuers)", "sec", "https://efts.sec.gov/LATEST/search-index", "ASML Intel TSMC Nvidia", 1),
            ("wallstengine", "twitter", "https://x.com/wallstengine", "wallstengine", 2),
            ("aleabitoreddit", "twitter", "https://x.com/aleabitoreddit", "aleabitoreddit", 2),
        ],
    },
    "Oil / Natural Gas / MLPs": {
        "group": "Energy",
        "sources": [
            ("HFI Research", "rss", "https://www.hfir.com/feed", "", 1),
            ("Rapidan Energy Group", "rss", "https://www.rapidanenergy.com/feed/", "", 1),
            ("Bison Interests", "rss", "https://www.bisoninterests.com/blog?format=rss", "", 1),
            ("RBN Energy", "rss", "https://rbnenergy.com/rss.xml", "", 1),
            ("OilPrice.com", "rss", "https://oilprice.com/rss/main", "", 1),
            ("EIA Today in Energy", "rss", "https://www.eia.gov/rss/todayinenergy.xml", "", 1),
            ("Javier Blas", "twitter", "https://x.com/JavierBlas", "JavierBlas", 1),
            ("S&P Global Commodity Insights", "rss", "https://www.spglobal.com/commodityinsights/en/rss-feed/oil", "", 1),
            ("Doomberg (energy)", "rss", "https://doomberg.substack.com/feed", "", 1),
            ("Reuters Energy", "rss", "https://www.reutersagency.com/feed/?best-topics=energy", "", 1),
        ],
    },
    "Utilities / Power / Renewables": {
        "group": "Energy",
        "sources": [
            ("Shift Key (Heatmap)", "rss", "https://heatmap.news/feed", "", 1),
            ("Heatmap News", "rss", "https://heatmap.news/feed", "", 1),
            ("Canary Media", "rss", "https://www.canarymedia.com/feed", "", 1),
            ("electroneconomics (Renewables Investor)", "rss", "https://electroneconomics.substack.com/feed", "", 1),
            ("CleanTechnica", "rss", "https://cleantechnica.com/feed/", "", 1),
            ("Electrek", "rss", "https://electrek.co/feed/", "", 1),
            ("Utility Dive", "rss", "https://www.utilitydive.com/feeds/news/", "", 1),
            ("DOE Office of Science", "rss", "https://science.osti.gov/news/rss", "", 1),
            ("PV Magazine", "rss", "https://www.pv-magazine.com/feed/", "", 1),
            ("Latitude Media", "rss", "https://www.latitudemedia.com/news?format=rss", "", 1),
        ],
    },
    "Mining / Precious Metals / Materials": {
        "group": "Materials",
        "sources": [
            ("Gold Newsletter", "rss", "https://goldnewsletter.com/feed/", "", 1),
            ("Brent Cook / Exploration Insights", "rss", "https://www.explorationinsights.com/pebble.asp?relid=rss", "", 1),
            ("The Northern Miner", "rss", "https://www.northernminer.com/feed/", "", 1),
            ("Kitco News", "rss", "https://www.kitco.com/rss/", "KitcoNewsNOW", 1),
            ("Mining.com", "rss", "https://www.mining.com/feed/", "", 1),
            ("Money Metals", "rss", "https://www.moneymetals.com/news/feed", "", 1),
            ("ChemAnalyst", "rss", "https://www.chemanalyst.com/rss", "", 2),
            ("S&P Global Metals & Mining", "rss", "https://www.spglobal.com/marketintelligence/en/rss/metals-mining", "", 1),
            ("Northern Miner Podcast", "youtube", "https://www.youtube.com/@NorthernMinerGroup", "@NorthernMinerGroup", 2),
            ("Mining Weekly", "rss", "https://www.miningweekly.com/rss", "", 1),
        ],
    },
    "Aerospace & Defense / Space": {
        "group": "Industrials",
        "sources": [
            ("Perun", "youtube", "https://www.youtube.com/@PerunAU", "@PerunAU", 1),
            ("Scott Manley", "youtube", "https://www.youtube.com/@scottmanley", "@scottmanley", 1),
            ("Everyday Astronaut", "youtube", "https://www.youtube.com/@EverydayAstronaut", "@EverydayAstronaut", 1),
            ("Breaking Defense", "rss", "https://breakingdefense.com/feed/", "", 1),
            ("Aviation Week", "rss", "https://aviationweek.com/awn/rss", "", 1),
            ("SpaceNews", "rss", "https://spacenews.com/feed/", "", 1),
            ("National Defense Magazine", "rss", "https://www.nationaldefensemagazine.org/rss", "", 1),
            ("DARPA News", "rss", "https://www.darpa.mil/rss.xml", "", 1),
            ("Aerospace Intelligence (space_osint)", "twitter", "https://x.com/space_osint", "space_osint", 2),
            ("Ars Technica Space", "rss", "https://arstechnica.com/science/space/feed/", "", 1),
        ],
    },
    "Biotech / Pharma / Medical Devices": {
        "group": "Healthcare",
        "sources": [
            ("Endpoints News", "rss", "https://endpts.com/feed/", "", 1),
            ("STAT News", "rss", "https://www.statnews.com/feed/", "", 1),
            ("Adam Feuerstein", "twitter", "https://x.com/adamfeuerstein", "adamfeuerstein", 1),
            ("Brad Loncar", "twitter", "https://x.com/bradloncar", "bradloncar", 1),
            ("AndyBiotech", "twitter", "https://x.com/AndyBiotech", "AndyBiotech", 1),
            ("Fierce Biotech", "rss", "https://www.fiercebiotech.com/rss/xml", "", 1),
            ("BioPharma Dive", "rss", "https://www.biopharmadive.com/feeds/news/", "", 1),
            ("BioCentury", "rss", "https://www.biocentury.com/rss", "", 1),
            ("Jacob Plieth (Evaluate)", "twitter", "https://x.com/JacobPlieth", "JacobPlieth", 1),
            ("Endpoints — The Readout", "rss", "https://endpts.com/feed/", "", 1),
        ],
    },
    "Consumer Discretionary / Retail": {
        "group": "Consumer",
        "sources": [
            ("Modest Proposal", "twitter", "https://x.com/modestproposal1", "modestproposal1", 1),
            ("As the Consumer Turns (Adam Josephson)", "rss", "https://adamjosephson.substack.com/feed", "", 1),
            ("Citrini Research", "rss", "https://www.citriniresearch.com/feed", "", 1),
            ("Maverick Equity Research", "rss", "https://www.maverickequityresearch.com/feed", "", 2),
            ("Retail Dive", "rss", "https://www.retaildive.com/feeds/news/", "", 1),
            ("The Bear Cave", "rss", "https://thebearcave.substack.com/feed", "", 1),
            ("Modern Retail", "rss", "https://www.modernretail.co/feed/", "", 1),
            ("Lean Luxe", "rss", "https://www.leanluxe.com/feed", "", 2),
            ("MoffettNathanson Consumer", "rss", "https://www.moffettnathanson.com/feed/", "", 2),
            ("Consumer fintwit slot", "twitter", "", "", 2),  # engine-discovered
        ],
    },
    "Consumer Staples / CPG": {
        "group": "Consumer",
        "sources": [
            ("Food Dive", "rss", "https://www.fooddive.com/feeds/news/", "", 1),
            ("Just Food", "rss", "https://www.just-food.com/feed/", "", 1),
            ("As the Consumer Turns (Adam Josephson)", "rss", "https://adamjosephson.substack.com/feed", "", 1),
            ("Beverage Digest", "rss", "https://www.beverage-digest.com/rss", "", 2),
            ("Morningstar Consumer", "rss", "https://www.morningstar.com/rss/news", "", 1),
            ("CPG Dive", "rss", "https://www.cpgdive.com/feeds/news/", "", 1),
            ("Snaxshot", "rss", "https://snaxshot.substack.com/feed", "", 2),
            ("The Food Institute", "rss", "https://foodinstitute.com/feed/", "", 1),
            ("McKinsey Consumer Packaged Goods", "rss", "https://www.mckinsey.com/industries/consumer-packaged-goods/rss", "", 1),
            ("Staples fintwit slot", "twitter", "", "", 2),  # engine-discovered
        ],
    },
    "Housing / Homebuilders / REITs": {
        "group": "Financials",
        "sources": [
            ("Calculated Risk (Bill McBride)", "rss", "https://www.calculatedriskblog.com/feeds/posts/default", "", 1),
            ("ResiClub (Lance Lambert)", "rss", "https://www.resiclubanalytics.com/feed", "LanceLambert13", 1),
            ("Logan Mohtashami", "twitter", "https://x.com/LoganMohtashami", "LoganMohtashami", 1),
            ("David Auerbach (Hoya Capital)", "twitter", "https://x.com/DAVIDAUERBACH73", "DAVIDAUERBACH73", 1),
            ("Nareit", "rss", "https://www.reit.com/rss.xml", "", 1),
            ("HousingWire", "rss", "https://www.housingwire.com/feed/", "", 1),
            ("The Real Deal", "rss", "https://therealdeal.com/feed/", "", 1),
            ("Globe St", "rss", "https://www.globest.com/feed/", "", 1),
            ("John Burns Research", "rss", "https://jbrec.com/feed/", "", 1),
            ("Cohen & Steers Insights", "rss", "https://www.cohenandsteers.com/insights/feed/", "", 2),
        ],
    },
    "Industrials / Robotics / Automation": {
        "group": "Industrials",
        "sources": [
            ("International Federation of Robotics", "rss", "https://ifr.org/news/rss", "", 1),
            ("Automation World", "rss", "https://www.automationworld.com/rss", "", 1),
            ("The Robot Report", "rss", "https://www.therobotreport.com/feed/", "", 1),
            ("Robotics 24/7", "rss", "https://www.robotics247.com/rss", "", 1),
            ("IEEE Spectrum Robotics", "rss", "https://spectrum.ieee.org/feeds/topic/robotics.rss", "", 1),
            ("Design News", "rss", "https://www.designnews.com/rss.xml", "", 1),
            ("Interesting Engineering", "rss", "https://interestingengineering.com/feed", "", 2),
            ("Rockwell Automation Journal", "rss", "https://www.rockwellautomation.com/en-us/company/news/the-journal.rss", "", 2),
            ("Assembly Magazine", "rss", "https://www.assemblymag.com/rss", "", 1),
            ("Robotics fintwit slot", "twitter", "", "", 2),  # engine-discovered
        ],
    },
    "Transport / Logistics / Shipping": {
        "group": "Industrials",
        "sources": [
            ("FreightWaves", "rss", "https://www.freightwaves.com/news/feed", "", 1),
            ("Freight Perspectives", "rss", "https://freightperspectives.substack.com/feed", "", 1),
            ("Logistics Strategies", "rss", "https://logisticsstrategies.substack.com/feed", "", 1),
            ("Supply Chain Dive", "rss", "https://www.supplychaindive.com/feeds/news/", "", 1),
            ("The Loadstar", "rss", "https://theloadstar.com/feed/", "", 1),
            ("Lloyd's List", "rss", "https://www.lloydslist.com/rss", "", 1),
            ("Sea-Intelligence", "twitter", "https://x.com/SeaIntelligence", "SeaIntelligence", 1),
            ("Journal of Commerce", "rss", "https://www.joc.com/rss.xml", "", 1),
            ("Aurelion Research (shipping)", "rss", "https://aurelionresearch.substack.com/feed", "", 2),
            ("gCaptain", "rss", "https://gcaptain.com/feed/", "", 1),
        ],
    },
    "Quantum / Photonics / Emerging Compute": {
        "group": "Tech",
        "sources": [
            ("Quantum Computing Report", "rss", "https://quantumcomputingreport.com/feed/", "", 1),
            ("The Quantum Insider", "rss", "https://thequantuminsider.com/feed/", "", 1),
            ("CSIS Strategic Technologies", "rss", "https://www.csis.org/programs/strategic-technologies-program/rss", "", 1),
            ("Anastasi in Tech", "youtube", "https://www.youtube.com/@AnastasiInTech", "@AnastasiInTech", 1),
            ("arXiv quant-ph", "arxiv", "http://export.arxiv.org/rss/quant-ph", "", 1),
            ("arXiv physics.optics", "arxiv", "http://export.arxiv.org/rss/physics.optics", "", 1),
            ("IEEE Spectrum (computing)", "rss", "https://spectrum.ieee.org/feeds/topic/computing.rss", "", 1),
            ("Photonics.com", "rss", "https://www.photonics.com/rss/news.aspx", "", 1),
            ("Nature Electronics", "rss", "https://www.nature.com/natelectron.rss", "", 1),
            ("Quantum researcher slot", "twitter", "", "", 2),  # engine-discovered
        ],
    },
    "Geopolitics / Supply Chain / Policy": {
        "group": "Macro",
        "sources": [
            ("ChinaTalk", "rss", "https://www.chinatalk.media/feed", "", 1),
            ("CHIPS Act / Commerce", "rss", "https://www.commerce.gov/feeds/news.xml", "", 1),
            ("Omdia (Informa Tech)", "rss", "https://omdia.tech.informa.com/rss", "", 2),
            ("SEMI.org", "rss", "https://www.semi.org/en/rss.xml", "", 1),
            ("European Chips Act", "rss", "https://digital-strategy.ec.europa.eu/en/rss.xml", "", 1),
            ("Alex Kokcharov", "twitter", "https://x.com/alexkokcharov", "alexkokcharov", 1),
            ("War on the Rocks", "rss", "https://warontherocks.com/feed/", "", 1),
            ("Lawfare", "rss", "https://www.lawfaremedia.org/feed", "", 1),
            ("CSIS", "rss", "https://www.csis.org/rss", "", 1),
            ("Reuters World", "rss", "https://www.reutersagency.com/feed/?best-topics=political-general", "", 1),
        ],
    },
    "Community Signals": {
        "group": "Macro",
        "sources": [
            ("Hacker News — semis", "hackernews", "", "semiconductor TSMC ASML EUV", 2),
            ("Hacker News — AI", "hackernews", "", "AI GPU inference LLM", 2),
            ("Hacker News — energy", "hackernews", "", "energy battery nuclear grid", 2),
            ("r/semiconductors", "reddit", "https://www.reddit.com/r/semiconductors", "semiconductors", 2),
            ("r/chipdesign", "reddit", "https://www.reddit.com/r/chipdesign", "chipdesign", 2),
            ("r/hardware", "reddit", "https://www.reddit.com/r/hardware", "hardware", 2),
            ("r/energy", "reddit", "https://www.reddit.com/r/energy", "energy", 2),
            ("r/biotech", "reddit", "https://www.reddit.com/r/biotech", "biotech", 2),
            ("r/RealEstate", "reddit", "https://www.reddit.com/r/RealEstate", "RealEstate", 2),
            ("r/stocks", "reddit", "https://www.reddit.com/r/stocks", "stocks", 2),
        ],
    },
}


# Form 4 (insider trade) sources — one per key ticker.
# No feed URL: the ticker in `handle` drives the EDGAR submissions lookup, and
# an empty url makes the seeder synthesize a unique internal:// key per ticker.
FORM4_SOURCES: list[tuple[str, str, str, str, int]] = [
    ("Form 4 — NVDA", "form4", "", "NVDA", 1),
    ("Form 4 — AMD",  "form4", "", "AMD",  1),
    ("Form 4 — ASML", "form4", "", "ASML", 1),
    ("Form 4 — MSFT", "form4", "", "MSFT", 1),
    ("Form 4 — AMZN", "form4", "", "AMZN", 1),
    ("Form 4 — TSM",  "form4", "", "TSM",  1),
]

# Patent sources via Google Patents XHR. handle = search keyword (sorted newest).
# Empty url → seeder synthesizes a unique internal:// key per keyword.
PATENT_SOURCES: list[tuple[str, str, str, str, int]] = [
    ("Patents: AI Accelerator",          "patent", "", "AI accelerator chip neural network", 1),
    ("Patents: EUV Lithography",         "patent", "", "EUV extreme ultraviolet lithography", 1),
    ("Patents: mRNA Delivery",           "patent", "", "mRNA lipid nanoparticle delivery", 1),
    ("Patents: Solid State Battery",     "patent", "", "solid state battery electrolyte", 1),
    ("Patents: Quantum Error Correction","patent", "", "quantum error correction qubit", 1),
]

# Smart-money & forecast sources (one global source each; no per-keyword feed).
SMART_MONEY_SOURCES: list[tuple[str, str, str, str, int]] = [
    ("Congressional Trades (Quiver)", "congress",   "", "congress", 1),
    ("Prediction Markets (Polymarket)", "prediction", "", "polymarket", 1),
    ("Prediction Markets (Kalshi)", "prediction", "", "kalshi", 1),
]

# Institutional / activist stake trackers (SC 13D/13G) — one per key ticker.
# handle = ticker; empty url → unique internal:// key per ticker.
_INST_TICKERS = [
    "NVDA", "AMD", "ASML", "MSFT", "AMZN", "TSM", "INTC", "AVGO", "MRVL",
    "ARM", "MU", "KLAC", "LRCX", "AMAT", "MRNA", "CRSP", "BEAM", "IONQ",
]
INSTITUTIONAL_SOURCES: list[tuple[str, str, str, str, int]] = [
    (f"13D/G — {t}", "institutional", "", t, 1) for t in _INST_TICKERS
]

# Dilution-risk trackers (S-1/S-3/424B offerings) — dilution-prone names.
_DILUTION_TICKERS = [
    "MRNA", "BNTX", "CRSP", "BEAM", "NTLA", "RXRX", "IONQ", "RGTI", "QUBT",
    "PLUG", "RUN", "BE", "ENPH", "FSLR",
]
DILUTION_SOURCES: list[tuple[str, str, str, str, int]] = [
    (f"Dilution — {t}", "dilution", "", t, 1) for t in _DILUTION_TICKERS
]

# 8-K material events + Form 144 (planned sells) — one per key ticker.
EVENT8K_SOURCES: list[tuple[str, str, str, str, int]] = [
    (f"8-K — {t}", "form8k", "", t, 1) for t in _INST_TICKERS
]
FORM144_SOURCES: list[tuple[str, str, str, str, int]] = [
    (f"Form 144 — {t}", "form144", "", t, 1) for t in _INST_TICKERS
]

# Biotech clinical-trial catalyst trackers. handle=sponsor name, tags=ticker.
_BIOTECH_COMPANIES = [
    ("Moderna", "MRNA"), ("BioNTech", "BNTX"), ("CRISPR Therapeutics", "CRSP"),
    ("Beam Therapeutics", "BEAM"), ("Intellia Therapeutics", "NTLA"),
    ("Recursion Pharmaceuticals", "RXRX"),
]
# handle encodes "Sponsor Name|TICKER" (seeder clobbers tags with industry name).
BIOTECH_SOURCES: list[tuple[str, str, str, str, int]] = [
    (f"Trials — {tk}", "clinicaltrial", "", f"{name}|{tk}", 1) for name, tk in _BIOTECH_COMPANIES
]

# Default ticker watchlists (list_name -> symbols).
WATCHLISTS: dict[str, list[str]] = {
    "EUV Supply Chain": ["ASML", "KLAC", "LRCX", "AMAT", "ONTO", "FORM", "BESI"],
    "AI Infrastructure": ["NVDA", "AMD", "INTC", "AVGO", "MRVL", "TSM", "ARM"],
    "Energy Transition": ["NEE", "ENPH", "FSLR", "BE", "PLUG", "RUN", "GEV"],
    "Biotech Catalyst": ["MRNA", "BNTX", "RXRX", "BEAM", "CRSP", "NTLA"],
    "Defense / Quantum": ["RTX", "LMT", "NOC", "IONQ", "RGTI", "QUBT"],
}
