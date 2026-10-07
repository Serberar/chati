"""Traducciones fijas de ropa, tejidos, estampados y colores para el editor
de fotos. qwen3:8b traducia "de tirantes" por "strapless" (lo contrario) y
"chaqueta vaquera" por "leather jacket": Kontext hacia exactamente lo que
decia la traduccion y la ropa salia mal (Sergio, foto real, 2026-10-07).

Se buscan en la peticion las expresiones de aqui, se le dan al planificador
como traducciones obligatorias (glossary_block) y, si aun asi escribe una
traduccion equivocada conocida, se corrige en la instruccion (fix_instruction).
"""
import re

# (expresion en español, traduccion, traducciones equivocadas que se corrigen)
# Las mas largas primero: "camiseta de tirantes" antes que "de tirantes".
TERMS: list[tuple[str, str, tuple[str, ...]]] = [
    # --- tirantes, escotes, mangas
    (r"camiseta de tirantes", "tank top", ()),
    (r"sin tirantes|palabra de honor", "strapless", ()),
    (r"de tirantes( finos)?|con tirantes( finos)?", "spaghetti-strap", ("strapless",)),
    (r"cuello vuelto|cuello alto|cuello cisne", "turtleneck", ()),
    (r"cuello (de |en )?pico|escote (de |en )?pico", "V-neck", ()),
    (r"escote barco", "boat neck", ()),
    (r"sin mangas", "sleeveless", ()),
    (r"manga corta", "short-sleeved", ("long-sleeved",)),
    (r"manga larga", "long-sleeved", ("short-sleeved",)),
    (r"hombros? al aire|hombros descubiertos", "off-the-shoulder", ()),
    # --- prendas
    (r"chaqueta vaquera|cazadora vaquera", "denim jacket", ("leather jacket",)),
    (r"camisa vaquera", "denim shirt", ()),
    (r"falda vaquera", "denim skirt", ()),
    (r"pantal[oó]n(es)? vaqueros?|vaqueros|tejanos", "blue jeans", ()),
    (r"cazadora de cuero|chaqueta de cuero|chupa de cuero|cazadora de piel|chaqueta de piel", "leather jacket", ()),
    (r"cazadora", "jacket", ()),
    (r"americana|bl[aá]zer", "blazer", ()),
    (r"chaqueta de punto|rebeca", "cardigan", ()),
    (r"chaleco", "vest", ()),
    (r"gabardina", "trench coat", ()),
    (r"plum[ií]fero", "puffer jacket", ()),
    (r"abrigo", "coat", ()),
    (r"sudadera con capucha", "hoodie", ()),
    (r"sudadera", "sweatshirt", ()),
    (r"jersey|su[eé]ter", "sweater", ("jersey",)),
    (r"ch[aá]ndal", "tracksuit", ()),
    (r"camiseta", "t-shirt", ()),
    (r"camisa de cuadros", "plaid button-up shirt", ()),
    (r"camisa", "button-up shirt", ()),
    (r"blusa", "blouse", ()),
    (r"polo", "polo shirt", ()),
    (r"vestido de noche|vestido de gala", "evening gown", ()),
    (r"vestido de novia", "wedding dress", ()),
    (r"vestido largo", "long maxi dress", ()),
    (r"vestido corto", "short dress", ()),
    (r"minifalda", "miniskirt", ()),
    (r"falda", "skirt", ()),
    (r"pantal[oó]n(es)? cortos?|bermudas", "shorts", ()),
    (r"pantal[oó]n(es)? de traje", "dress trousers", ()),
    (r"mallas|leggins|leggings", "leggings", ()),
    (r"(?<!a )medias", "tights", ()),
    (r"peto", "dungarees", ()),
    (r"pijama", "pajamas", ()),
    (r"albornoz", "bathrobe", ()),
    (r"delantal", "apron", ()),
    (r"esmoquin", "tuxedo", ()),
    (r"traje de chaqueta", "suit", ()),
    (r"pajarita", "bow tie", ()),
    (r"corbata", "necktie", ()),
    (r"bufanda", "scarf", ()),
    (r"pañuelo", "neckerchief", ()),
    (r"gorro de lana", "knitted wool beanie", ()),
    (r"gorra", "baseball cap", ()),
    (r"zapatillas( de deporte)?|deportivas", "sneakers", ("slippers",)),
    (r"zapatos de tac[oó]n|tacones", "high heels", ()),
    (r"chanclas", "flip-flops", ()),
    (r"cintur[oó]n", "belt", ()),
    # --- tejidos y estampados
    (r"de cuadros|a cuadros", "plaid", ()),
    (r"de rayas|a rayas", "striped", ()),
    (r"de lunares|a lunares", "polka-dot", ()),
    (r"de flores|estampado de flores|floreado", "floral print", ()),
    (r"lentejuelas", "sequined", ()),
    (r"encaje", "lace", ()),
    (r"terciopelo", "velvet", ()),
    (r"pana", "corduroy", ()),
    (r"de ante", "suede", ()),
    (r"lino", "linen", ()),
    (r"seda", "silk", ()),
    (r"de punto", "knitted", ()),
    (r"de lana", "wool", ()),
    (r"de cuero", "leather", ()),
    (r"(?<!de )vaquera", "denim", ("leather",)),
    # --- colores que confunde
    (r"granate|burdeos", "burgundy", ()),
    (r"beis|beige", "beige", ()),
    (r"caqui", "khaki", ()),
    (r"azul marino", "navy blue", ()),
    (r"azul cielo|celeste", "light blue", ()),
    (r"verde botella", "dark green", ()),
    (r"verde oliva", "olive green", ()),
    (r"mostaza", "mustard yellow", ()),
    (r"lila", "lilac", ()),
    (r"morad[oa]", "purple", ()),
    (r"fucsia", "fuchsia", ()),
    (r"turquesa", "turquoise", ()),
    (r"salm[oó]n", "salmon pink", ()),
    (r"crema", "cream", ()),
    (r"dorad[oa]", "gold", ()),
    (r"platead[oa]", "silver", ()),
]

_COMPILED = [(re.compile(rf"\b({pattern})\b", re.IGNORECASE), en, wrong) for pattern, en, wrong in TERMS]


def find(request: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """(texto encontrado, traduccion, equivocadas) de cada expresion de la
    peticion; si una mas larga ya cubre ese trozo, la corta no cuenta."""
    taken: list[tuple[int, int]] = []
    found = []
    for rx, en, wrong in _COMPILED:
        for m in rx.finditer(request or ""):
            a, b = m.span()
            if any(a < y and x < b for x, y in taken):
                continue
            taken.append((a, b))
            found.append((m.group(0), en, wrong))
    return found


GLOSSARY_BLOCK = """Traducciones OBLIGATORIAS de lo que pide (usalas tal cual, no otras):
{items}
"""


def glossary_block(request: str) -> str:
    found = find(request)
    if not found:
        return ""
    return GLOSSARY_BLOCK.format(items="\n".join(f'- "{es}" = "{en}"' for es, en, _ in found))


def fix_instruction(request: str, instruction: str) -> str:
    """Corrige las traducciones equivocadas conocidas si el planificador las
    escribio a pesar del glosario (y no la buena)."""
    for _, en, wrong in find(request):
        if en.lower() in instruction.lower():
            continue
        for bad in wrong:
            rx = re.compile(rf"\b{re.escape(bad)}\b", re.IGNORECASE)
            if rx.search(instruction):
                instruction = rx.sub(en, instruction, count=1)
                break
    return instruction
