"""Offline, source-driven prompt preparation. Never invents new forms or calls an API.

Visual primitives describe geometry of an existing matrix row; ingredient-specific
notes and explicit overrides determine material. These are drafts, not validated art.
"""
import re

VERSION = 'matrix-composer-1'

MATERIALS = {
    'tomato': 'Red flesh, glossy juice and translucent seed gel; thin curved red skin on outer surfaces',
    'carrot': 'Firm orange flesh with a slightly lighter central core and faint longitudinal fibers',
    'onion': 'Translucent ivory concentric layers; papery yellow husk only on the unpeeled whole bulb',
    'potato': 'Pale cream starchy flesh; thin tan skin on the unpeeled whole tuber',
    'butter': 'Pale yellow butter with a smooth waxy surface and clean knife-cut edges',
    'olive oil': 'Transparent golden-yellow liquid with a restrained glossy reflection',
    'cheese': 'Pale yellow semi-hard cheese with a smooth dense cut surface',
    'garlic': 'Creamy ivory clove flesh; white papery layers on the unpeeled head or cloves',
    'egg': 'A natural matte eggshell around a gently asymmetric oval egg',
    'chicken breast': 'Moist pale pink meat with fine muscle fibers and a smooth tapered surface',
    'beef steak': 'Deep red muscle fibers with small creamy fat seams',
    'cod': 'Moist opaque ivory fish flesh with broad flake-like muscle layers',
    'rice': 'Dry slender ivory long-grain rice kernels',
    'plain flour': 'Fine soft off-white flour with a matte powdery texture',
    'sugar': 'Tiny white translucent granulated sugar crystals, 0.3–0.8 mm across',
    'salt': 'White angular table-salt crystals, 0.5–1 mm across',
    'basil': 'Fresh green supple leaves with curved veins and slightly glossy surfaces',
    'soy sauce': 'Transparent very dark brown liquid with a glossy amber edge',
}

# Specific defaults for rows whose notes only contained exclusions. These are
# consulted after the individual row's positive texture notes, never by category.
FALLBACK_MATERIALS = {
    'anchovies':'Moist pink-brown anchovy flesh with an oily sheen and fine fish fibers',
    'apple jelly':'Smooth translucent amber apple jelly with a soft glossy surface',
    'baking mix':'Fine matte cream-colored dry baking mixture',
    'baking powder':'Very fine matte white baking powder',
    'baking soda':'Fine matte white bicarbonate powder',
    'breadsticks':'Golden-brown crisp baked bread crust with a dry porous interior',
    'brie':'Ivory creamy cheese enclosed by a thin soft white bloomy rind',
    'buckwheat flour':'Fine light gray-beige flour with subtle darker flecks',
    'cajun seasoning':'Reddish-brown dry spice mixture with tiny dark herb flecks',
    'cake flour':'Very fine soft ivory-white flour',
    'cake mix':'Fine pale cream dry cake mixture',
    'calf liver':'Smooth moist reddish-brown dense liver tissue',
    'camembert':'Soft ivory cheese with a delicate white bloomy rind',
    'canned tomatoes':'Soft moist red peeled tomato flesh with glossy juice',
    'capers':'Small olive-green caper buds with folded uneven surfaces',
    'caster sugar':'Fine white crystalline caster sugar',
    'chaat masala':'Fine tan-brown dry spice mixture with small darker flecks',
    'chestnuts':'Dense pale beige cooked chestnut flesh with a slightly crumbly surface',
    'chicken thigh':'Moist pale pink chicken meat with creamy fat seams and visible fibers',
    'chickpea flour':'Fine matte pale yellow chickpea flour',
    'chilli lime seasoning':'Dry orange-red chilli mixture with light salt grains',
    'chilli powder':'Fine matte brick-red chilli powder',
    'cinnamon':'Warm brown finely ground cinnamon with a dry matte texture',
    'cocoa powder':'Fine deep brown matte cocoa powder',
    'colby':'Smooth firm yellow-orange cheese with small irregular openings',
    'corned beef':'Prepared pink-red beef with visible moist fibers and pale fat',
    'courgette flowers':'Delicate folded yellow-orange petals with green bases',
    'crab':'Moist white crab flesh with subtle red-orange accents and flaky fibers',
    'cream':'Smooth opaque ivory-white liquid cream',
    'cream of tartar':'Fine matte white cream-of-tartar powder',
    'creole seasoning':'Red-brown dry seasoning with small green and dark herb flecks',
    'curry powder':'Fine golden ochre dry curry powder',
    'custard powder':'Fine pale yellow dry custard powder',
    'double cream':'Thick smooth opaque ivory-white liquid cream',
    'dried citrus peel':'Dry wrinkled orange-yellow peel with pale porous inner pith',
    'egg substitute':'Smooth pale yellow prepared liquid egg substitute',
    'fish seasoning':'Fine pale tan dry seasoning with visible small herb flecks',
    'five-spice powder':'Fine warm brown dry ground spice mixture',
    'flavoured jelly':'Translucent glossy red set jelly with softly elastic edges',
    'garam masala':'Fine warm brown dry aromatic spice mixture',
    'garlic powder':'Fine pale cream matte garlic powder',
    'gluten-free flour':'Fine soft off-white dry flour',
    'glutinous rice flour':'Very fine soft opaque white rice flour',
    'graham cracker crust':'Golden-brown pressed crumb texture with small crumbly edges',
    'gravy mix':'Fine light brown dry gravy mixture',
    'green peppercorns':'Small moist muted green peppercorns with subtly wrinkled skins',
    'gruyere':'Firm pale golden cheese with a smooth dense cut face',
    'guar gum':'Fine matte ivory-white guar-gum powder',
    'harissa seasoning':'Brick-red dry spice mixture with small dark flecks',
    'honey':'Clear golden amber honey with a thick glossy liquid surface',
    'ice cream':'Smooth creamy ivory vanilla ice cream with soft ridges',
    'icing sugar':'Extremely fine soft white powdered sugar',
    'instant pudding mix':'Fine pale cream-yellow dry pudding mixture',
    'juniper berries':'Dry deep blue-purple berries with a dusty bloom and wrinkled skin',
    'kasar cheese':'Firm pale yellow cheese with a smooth dense cut surface',
    'kashmiri chilli powder':'Fine vivid red matte ground chilli',
    'kataifi pastry':'Very thin pale cream pastry strands, loosely intertwined',
    'lavender':'Small purple flower buds on slender muted green stems',
    'lemonade mix':'Fine pale yellow-white dry drink mixture',
    'lemon pepper':'Yellow-tan seasoning with visible black pepper flecks',
    'macaroni and cheese mix':'Dry curved pale macaroni with separate fine yellow cheese mixture as specified by the visual form',
    'malted milk powder':'Fine light cream-beige dry malted milk powder',
    'meringue':'Opaque white baked meringue with a dry delicate porous surface',
    'milk powder':'Fine pale cream dry milk powder',
    'mixed seafood':'Distinct moist shrimp, squid and shellfish flesh with individual natural contours',
    'mixed spice':'Fine warm brown dry baking-spice mixture',
    'monterey jack':'Smooth creamy pale yellow semi-hard cheese',
    'mustard powder':'Fine matte ochre-yellow ground mustard',
    'oat flour':'Fine pale cream flour with subtle beige grain flecks',
    'onion powder':'Fine dry ivory-beige onion powder',
    'orange drink powder':'Fine light orange dry drink mixture',
    'orange extract':'Clear pale amber liquid orange extract',
    'oyster mushrooms':'Pale gray-beige oyster mushrooms with soft fan-shaped caps and fine cream gills',
    'palm sugar':'Caramel-brown palm sugar with a dense slightly grainy texture',
    'paprika':'Fine vivid brick-red matte paprika powder',
    'parmesan':'Pale straw-colored hard cheese with a dry granular texture',
    'passion fruit':'Glossy golden-yellow pulp with many small dark seeds',
    'pecorino':'Pale cream firm cheese with a dry fine granular cut texture',
    'pectin':'Fine matte pale cream pectin powder',
    'pesto sauce mix':'Dry green-brown herb and seasoning fragments with a loose granular texture',
    'pickled jalapenos':'Moist olive-green chilli flesh with thin glossy skins and pale seeds',
    'pickling spice':'Distinct brown seeds, dark peppercorns and small dry leaf fragments',
    'pineapple':'Moist golden-yellow pineapple flesh with fibrous cut faces',
    'pistachios':'Pale green pistachio kernels with thin purple-brown skin patches',
    'pomegranate':'Glossy translucent ruby-red juicy arils with pale inner seeds',
    'potato starch':'Extremely fine soft white dry starch',
    'prawns':'Moist translucent pale pink shrimp flesh with curved segmented contours',
    'processed cheese':'Smooth uniform pale yellow cheese with soft clean cut edges',
    'prosciutto':'Thin translucent rose-red cured meat with creamy white fat ribbons',
    'protein powder':'Fine matte ivory-beige dry protein powder',
    'provolone':'Smooth firm pale yellow cheese with a dense cut surface',
    'ras el hanout':'Fine ochre-brown dry ground spice mixture',
    'redcurrant jelly':'Smooth translucent ruby-red glossy jelly',
    'refrigerated biscuit dough':'Soft moist pale cream unbaked biscuit dough with subtle floury patches',
    'rice flour':'Fine matte opaque white rice flour',
    'roasted red peppers':'Soft glossy deep red prepared pepper flesh with thin wrinkled skin',
    'royal icing sugar':'Very fine soft white dry icing mixture',
    'rye flour':'Fine gray-beige flour with darker grain flecks',
    'saffron':'Slender dry dark orange-red saffron threads with slightly flared ends',
    'seafood seasoning':'Fine red-tan dry spice mixture with small dark flecks',
    'seasoned salt':'Tiny pale orange-tan crystals mixed with fine reddish spice flecks',
    'self-raising flour':'Fine soft off-white dry flour',
    'semolina':'Many tiny pale golden semolina granules with a subtly coarse texture',
    'sichuan peppercorns':'Small dry red-brown open husks with irregular folded edges',
    'smoked paprika':'Fine deep brick-red matte ground paprika',
    'soy flour':'Fine matte light cream-yellow soy flour',
    'spelt flour':'Fine light beige flour with subtle darker grain flecks',
    'squid':'Moist pearly ivory squid flesh with a smooth elastic surface',
    'steak seasoning':'Coarse tan, brown and dark spice particles with distinct salt and pepper flecks',
    'stem ginger':'Moist golden-yellow ginger flesh with light fibers and a glossy syrup coating',
    'stewing beef':'Deep red beef flesh with visible fibers and small creamy fat seams',
    'sugar substitute':'Fine white dry granulated sweetener',
    'taco seasoning':'Fine red-brown ground seasoning with small dark flecks',
    'tandoori masala':'Fine vivid red-orange dry ground spice mixture',
    'tenderstem broccoli':'Small dark green branching florets on long smooth green stems',
    'tomatillos':'Firm pale green moist flesh beneath smooth green skin',
    'truffle':'Dense dark brown truffle flesh and an irregular knobbly outer surface',
    'turmeric':'Fine bright ochre-orange matte ground turmeric',
    'wakame':'Soft glossy dark green seaweed with thin wavy folded surfaces',
    'wholemeal flour':'Fine tan-beige flour with visibly darker bran flecks',
    'yeast':'Tiny dry beige yeast granules with a matte surface',
    'yogurt':'Smooth thick opaque ivory-white natural yogurt with soft rounded folds',
}


def clean(text):
    replacements = {
        'Save escape character': 'Delicate individual shoots remain distinct',
        'Save the skin': 'Retain the skin', 'Save the fried crust': 'Retain the crisp fried crust',
        'Thin Husk': 'Thin papery husk', 'cooked: an ingredient prepared before being added to a recipe': '',
        'original ingredient before adding to recipe': '',
        'Option for preparing an ingredient for a recipe': '',
        'Generalized name': '', 'Show characteristic shape': '',
        'No particle counting': '', 'Without counting crystals': '', 'Without counting shavings': '',
        'Maintain the shape and color of the grains': '',
        'Maintain the leaf shape of a particular herb': '',
        'Maintain the leaf shape of a specific herb': '',
    }
    for before, after in replacements.items():
        text = text.replace(before, after)
    return text.strip(' ;.')


def prepare(form):
    name, source, state, amount, dimensions, direction, arrangement, note = form['requirements_en']
    low = state.casefold()
    cooked = bool(re.search(r'cooked|boiled|roasted|fried|grilled|baked|canned', low))
    cut = bool(re.search(r'cut|cube|chopped|minced|grated|shred|slice|strip|julienne|round|pieces|halves|section',low))
    liquid = form['container_policy'] == 'required'
    criteria, details, prep = [], [], []
    for part in re.split(r';\s*', note):
        part = clean(part)
        if cut:
            # Notes sometimes repeat the intact product's dimensions. The cut row's
            # own size is authoritative; never turn 1 cm dice into 20 cm roots.
            part=re.sub(r'(?:\b(?:length|leaf length|width|height|thickness|edge)\s*)?Ø?\d+(?:[.,]\d+)?(?:\s*[–×x-]\s*\d+(?:[.,]\d+)?)*\s*(?:cm|mm)', '',part,flags=re.I).strip(' ;.')
        if not part or re.search(r'related starting ingredient|same meaning|connection of the original|re-generation|unless specified|without counting|no slice counting|not a single|original form', part, re.I):
            continue
        # Explicit source statements about preparation, rather than generic prohibitions.
        if re.search(r'without skin|skinless', part, re.I):
            prep.append('Peeled or skinless flesh')
        if re.search(r'boneless|without bones', part, re.I):
            prep.append('Boneless prepared ingredient')
        if re.search(r'without wrapper|without packaging', part, re.I):
            prep.append('Unwrapped ingredient')
        if re.search(r'without shell', part, re.I):
            prep.append('Shell removed')
        if not cooked and re.search(r'after cooking|cooked |cooked:', part, re.I):
            continue
        exclusion=re.search(r'\b(no |not |without |don.t |do not |was selected|is selected|selected;)', part, re.I)
        if exclusion:
            criteria.append(part)
            positive=part[:exclusion.start()].strip(' ,;.')
            if positive and not re.search(r'generalized|original|related|selected',positive,re.I):
                details.append(positive)
        else:
            details.append(part)
    material = MATERIALS.get(source, '')
    if cooked and source == 'rice':
        material = 'Soft opaque white enlarged long grains, visibly separate with light moist highlights'
    if cooked and source == 'chicken breast':
        material = 'Opaque white cooked meat with distinct fibers and moist cut surfaces'
    if source == 'butter' and 'melted' in low:
        material = 'Glossy translucent golden-yellow melted butter, forming a smooth level liquid surface'
    if source == 'butter' and 'softened' in low:
        material = 'Soft pale yellow butter with rounded edges and a shallow pressure mark, retaining its volume'
    if source == 'onion' and 'whole' not in low:
        material = 'Moist ivory onion flesh with distinct curved translucent layers'
    if source == 'potato' and 'whole' not in low:
        material = 'Pale cream moist starchy cut flesh'
    if source == 'garlic' and not any(x in low for x in ('unpeeled', 'whole head')):
        material = 'Smooth creamy ivory garlic flesh with subtle fibers'
    if source == 'egg':
        if 'cracked' in low:
            material = 'One intact round golden yolk suspended in clear glossy egg white'
        elif 'beaten' in low:
            material = 'Uniform golden-yellow liquid beaten egg with tiny bubbles'
        elif cooked:
            material = 'Dense opaque smooth white egg white' if 'peeled' in low and 'whole' not in low and 'round' not in low else 'Dense opaque smooth white egg white and firm golden-yellow yolk on exposed cuts'
    if not material:
        material = '. '.join(details) if details else FALLBACK_MATERIALS.get(source, f'The recognizable natural color and surface texture of {name.lower()} in this preparation')
    elif details:
        material += '. ' + '. '.join(details)

    geometry = state + '. Dimensions of the depicted portion: ' + dimensions
    if liquid:
        geometry += '. The ingredient fills its visible vessel; its surface and fill level can be seen inside the rim'
    elif 'section' in low and ('one half' in low or amount == '1 half'):
        geometry += '. A substantial three-dimensional half with a broad exposed cut face and visible depth behind the cut; the continuous outer surface curves away from the cut edge'
        criteria += ['A thin disc instead of a volumetric half', 'Hidden cut surface', 'Extra whole ingredient next to the half']
    elif 'halves' in low:
        geometry += '. Matching three-dimensional halves with clearly exposed interiors and curved outer surfaces'
    elif 'half rings' in low:
        geometry += '. Curved layered semicircular arcs with open ends and a visible thickness'
    elif 'rings' in low:
        geometry += '. Open circular rings with clear holes and thin layered edges'
    elif re.search(r'round|slice|sheets', low):
        geometry += '. Thin cut pieces with broad faces and narrow side edges, following the natural outline of the ingredient'
    elif 'wedges' in low:
        geometry += '. Tapered radial wedges with two cut faces meeting at an inner edge and a curved outer surface'
    elif 'cube' in low:
        geometry += '. Small knife-cut pieces with multiple visible cut planes and a short thickness, preserving the material structure of this ingredient'
    elif re.search(r'julienne|strips|matchsticks', low):
        geometry += '. Slender elongated cut strips, with visible short cross-sections and lengths following the matrix'
    elif re.search(r'grated|shavings|shredded', low):
        geometry += '. Thin irregular shreds or curls with narrow edges and loose overlapping strands'
    elif re.search(r'minced|chopped|pieces|fragments|crushed', low):
        geometry += '. Naturally uneven small cut fragments with broken or knife-cut edges appropriate to this ingredient'
    elif 'powder' in low:
        geometry += '. A low mound of fine powder with soft sloping sides and a softly irregular outline'
    elif 'crystals' in low:
        geometry += '. A low loose mound of tiny angular crystals, with a slightly granular outline'
    elif 'dry leaves' in low:
        geometry += '. A small loose mound of dry fragmented leaves, with curled brittle edges'
    elif re.search(r'grains|seeds', low):
        geometry += '. A low loose mound of many distinct grains or seeds of this ingredient, with scattered individual units at the edge'
    elif re.search(r'paste|puree|pureed|thick|sauce', low):
        geometry += '. One cohesive soft portion with an organic outline and the consistency specified in the source'

    if source == 'carrot' and 'whole' in low:
        geometry += '. An elongated tapered orange root, broad and rounded at the crown, narrowing continuously to a slim tip'
    if source == 'onion' and 'whole' in low:
        geometry += '. A rounded bulb with a small dense root base and short dry pointed neck'
    if source == 'potato' and 'whole' in low:
        geometry += '. An irregular oval tuber with shallow eyes and a subtly uneven skin'
    if source == 'butter' and ('block' in low or low == 'piece'):
        geometry += '. A solid rectangular butter piece with straight cut faces and visible height'
    if source == 'rice':
        geometry += '. Many individual slender elongated kernels overlap loosely, keeping recognizable grain silhouettes'
    if source in ('sugar', 'salt'):
        geometry += '. A compact mound made of many tiny crystals, with individual crystals indicated only at the edges and surface'
    if source == 'plain flour':
        geometry += '. A fine powder mound with soft sloping sides and subtle surface ridges'
    if source == 'tomato' and low == 'cubes':
        geometry = 'Small soft irregular tomato morsels with asymmetrical silhouettes, ragged cut edges and short visible thickness; about 1–1.5 cm across'
        material = 'Moist red flesh with small glossy gel patches and occasional pale seeds; only some pieces carry a curved thin red skin patch'
        criteria += ['Large wedges', 'Uniform rigid geometric cubes', 'Complete seed chambers on every small piece']

    if liquid:
        vessel = 'bottle' if 'bottle' in low else 'glass' if 'glass' in low else 'spoon' if 'spoon' in low else 'can' if 'can' in low else 'small bowl'
        container = f'One plain unmarked {vessel}, fully visible as part of this ingredient asset, with its rim and complete outer silhouette visible'
        if vessel in ('bottle','glass'):
            container += '; transparent glass reveals the contents'
        if vessel == 'small bowl':
            container += '; plain white ceramic'
        composition = clean(arrangement) + '. The vessel and contents form one isolated object before addition to a recipe'
    else:
        container = ''
        composition = re.sub(r'\bwithout container\b', 'as a free-standing ingredient', clean(arrangement), flags=re.I)
        composition += '. Only the ingredient itself forms the isolated asset, at the specified visual portion'
    preparation = 'Ingredient prepared before adding to a recipe. Visual preparation: ' + state
    if prep:
        preparation += '. ' + '. '.join(dict.fromkeys(prep))
    view = clean(direction)
    if not re.search(r'view|camera|above|from the side|cut face', view, re.I):
        view = 'Elevated three-quarter view. Ingredient orientation: ' + view
    return {
        'blocks':dict(subject=name,preparation=preparation,geometry=geometry,material=material,
                      portion=f'Visual portion: {amount}. {dimensions}',composition=composition,view=view,container=container),
        'rejection_rules':'\n'.join(dict.fromkeys(criteria)),
        'version':VERSION,
    }
