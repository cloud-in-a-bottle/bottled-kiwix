import attr


@attr.s(auto_attribs=True, frozen=True)
class Preset:
    """A common library, offered as a one-click button. Resolved to the newest matching catalog entry."""

    key: str
    label: str
    description: str
    name: str
    flavour: str


PRESETS = (
    Preset(
        "wikipedia-top",
        "Wikipedia: top 100",
        "The 100 most read articles with images. Good for trying it out.",
        "wikipedia_en_100",
        "",
    ),
    Preset(
        "wikipedia-simple",
        "Simple English Wikipedia",
        "All of Simple English Wikipedia, with images.",
        "wikipedia_en-simple_all",
        "maxi",
    ),
    Preset(
        "wikipedia-mini",
        "Wikipedia: intros",
        "Every English article, introduction only, no images.",
        "wikipedia_en_all",
        "mini",
    ),
    Preset(
        "wikipedia-nopic",
        "Wikipedia: full text",
        "Every English article in full, without images.",
        "wikipedia_en_all",
        "nopic",
    ),
    Preset(
        "wikipedia-maxi",
        "Wikipedia: everything",
        "Every English article in full, with images.",
        "wikipedia_en_all",
        "maxi",
    ),
    Preset("wiktionary", "Wiktionary", "English dictionary.", "wiktionary_en_all", "nopic"),
    Preset("wikivoyage", "Wikivoyage", "Travel guides, with images.", "wikivoyage_en_all", "maxi"),
    Preset("wikiquote", "Wikiquote", "Quotations, with images.", "wikiquote_en_all", "maxi"),
    Preset("wikibooks", "Wikibooks", "Free textbooks, with images.", "wikibooks_en_all", "maxi"),
    Preset("ifixit", "iFixit", "Repair guides.", "ifixit_en_all", ""),
    Preset(
        "stackoverflow", "Stack Overflow", "All Stack Overflow questions and answers.", "stackoverflow.com_en_all", ""
    ),
    Preset("gutenberg", "Project Gutenberg", "Around 70,000 public domain books.", "gutenberg_en_all", ""),
)
