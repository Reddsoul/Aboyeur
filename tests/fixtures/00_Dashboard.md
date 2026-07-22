# Recipe Vault

**64,045** recipes · **76** cuisines · **74,991** unique ingredients

---

## Browse by Cuisine

- [[_Cuisines/American|American]] (16980 recipes)
- [[_Cuisines/Ancient_Roman|Ancient Roman]] (12 recipes)
- [[_Cuisines/Armenian|Armenian]] (2 recipes)
- [[_Cuisines/Asian_Fusion|Asian Fusion]] (2 recipes)
- [[_Cuisines/Austrian|Austrian]] (33 recipes)
- [[_Cuisines/Belgian|Belgian]] (27 recipes)
- [[_Cuisines/Bengali|Bengali]] (2 recipes)
- [[_Cuisines/Bosnian|Bosnian]] (1 recipes)
- [[_Cuisines/Brazilian|Brazilian]] (4 recipes)
- [[_Cuisines/British|British]] (811 recipes)
- [[_Cuisines/British_Indian|British Indian]] (1 recipes)
- [[_Cuisines/British-Indian|British-Indian]] (1 recipes)
- [[_Cuisines/Canadian|Canadian]] (201 recipes)
- [[_Cuisines/Caribbean|Caribbean]] (1 recipes)
- [[_Cuisines/Central_Asian|Central Asian]] (1 recipes)
- [[_Cuisines/Chinese|Chinese]] (853 recipes)
- [[_Cuisines/Continental|Continental]] (1 recipes)
- [[_Cuisines/Creole|Creole]] (5 recipes)
- [[_Cuisines/Cuban|Cuban]] (2 recipes)
- [[_Cuisines/Czech|Czech]] (2 recipes)
- [[_Cuisines/Dalmatian|Dalmatian]] (1 recipes)
- [[_Cuisines/Danish|Danish]] (4 recipes)
- [[_Cuisines/Dutch|Dutch]] (312 recipes)
- [[_Cuisines/East_Indian|East Indian]] (2 recipes)
- [[_Cuisines/Egyptian|Egyptian]] (5 recipes)
- [[_Cuisines/English|English]] (6629 recipes)
- [[_Cuisines/European|European]] (25 recipes)
- [[_Cuisines/Flemish|Flemish]] (1 recipes)
- [[_Cuisines/French|French]] (5239 recipes)
- [[_Cuisines/German|German]] (144 recipes)
- [[_Cuisines/Goan|Goan]] (1 recipes)
- [[_Cuisines/Greek|Greek]] (6 recipes)
- [[_Cuisines/Historical|Historical]] (85 recipes)
- [[_Cuisines/Hungarian|Hungarian]] (19 recipes)
- [[_Cuisines/Indian|Indian]] (1655 recipes)
- [[_Cuisines/Indian_Assamese|Indian (Assamese)]] (1 recipes)
- [[_Cuisines/Indian-American|Indian-American]] (1 recipes)
- [[_Cuisines/Indian-Fusion|Indian-Fusion]] (1 recipes)
- [[_Cuisines/Indian-Italian_Fusion|Indian-Italian Fusion]] (1 recipes)
- [[_Cuisines/Indonesian|Indonesian]] (7 recipes)
- [[_Cuisines/International|International]] (25693 recipes)
- [[_Cuisines/Irish|Irish]] (42 recipes)
- [[_Cuisines/Italian|Italian]] (614 recipes)
- [[_Cuisines/Japanese|Japanese]] (297 recipes)
- [[_Cuisines/Jewish|Jewish]] (1632 recipes)
- [[_Cuisines/Korean|Korean]] (164 recipes)
- [[_Cuisines/Maltese|Maltese]] (1 recipes)
- [[_Cuisines/Manx|Manx]] (1 recipes)
- [[_Cuisines/Medieval_European|Medieval European]] (2 recipes)
- [[_Cuisines/Mexican|Mexican]] (31 recipes)
- [[_Cuisines/Mexican-Indian_Fusion|Mexican-Indian Fusion]] (1 recipes)
- [[_Cuisines/Middle_Eastern|Middle Eastern]] (3 recipes)
- [[_Cuisines/Nepalese|Nepalese]] (1 recipes)
- [[_Cuisines/Nordic|Nordic]] (123 recipes)
- [[_Cuisines/Norwegian|Norwegian]] (11 recipes)
- [[_Cuisines/Pakistani|Pakistani]] (2 recipes)
- [[_Cuisines/Pennsylvania_Dutch|Pennsylvania Dutch]] (173 recipes)
- [[_Cuisines/Persian|Persian]] (2 recipes)
- [[_Cuisines/Peruvian|Peruvian]] (1 recipes)
- [[_Cuisines/Polish|Polish]] (28 recipes)
- [[_Cuisines/Portuguese|Portuguese]] (7 recipes)
- [[_Cuisines/Reference|Reference]] (269 recipes)
- [[_Cuisines/Roman|Roman]] (867 recipes)
- [[_Cuisines/Russian|Russian]] (54 recipes)
- [[_Cuisines/Scandinavian|Scandinavian]] (2 recipes)
- [[_Cuisines/Scotch|Scotch]] (2 recipes)
- [[_Cuisines/Scottish|Scottish]] (9 recipes)
- [[_Cuisines/Serbian|Serbian]] (11 recipes)
- [[_Cuisines/Spanish|Spanish]] (724 recipes)
- [[_Cuisines/Sri_Lankan|Sri Lankan]] (5 recipes)
- [[_Cuisines/Swedish|Swedish]] (21 recipes)
- [[_Cuisines/Swiss|Swiss]] (33 recipes)
- [[_Cuisines/Thai|Thai]] (104 recipes)
- [[_Cuisines/Tibetan|Tibetan]] (1 recipes)
- [[_Cuisines/Turkish|Turkish]] (18 recipes)
- [[_Cuisines/Welsh|Welsh]] (15 recipes)

---

## Stats

```dataview
TABLE length(rows) AS "Recipes", rows.source[0] as "Books"
FROM #recipe
GROUP BY cuisine
SORT length(rows) DESC
```

---

## Recently Added

```dataview
LIST
FROM #recipe
SORT file.mtime DESC
LIMIT 20
```

---

## Ingredient Browser

→ [[_Ingredients/Browse|Browse by Ingredient]]

### Quick ingredient search

```dataview
TABLE source as Book, cuisine
FROM #recipe
WHERE contains(ingredients, "garlic")
SORT file.name ASC
LIMIT 30
```

---

## All Books / Sources

```dataview
TABLE rows.file.link AS Recipes, length(rows) AS Count
FROM #recipe
GROUP BY source
SORT length(rows) DESC
```
