// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The texts of the shared parts: the shell, the appearance menu and the
 * buttons every page has. Each app adds its own table with useStrings; a key
 * lives in one table only. English is the source language and every key
 * carries all four languages.
 *
 * German uses "du", Dutch "je", French "vous".
 */

const s = (en, de, nl, fr) => ({ en, de, nl, fr });

/** Keys the shell reads that each app's own table must define. */
export const APP_KEYS = Object.freeze(["app.name", "app.sub"]);

export const SHARED_STRINGS = {
  "app.skip": s("Skip to content", "Zum Inhalt springen", "Naar inhoud", "Aller au contenu"),

  "nav.main": s("Main navigation", "Hauptnavigation", "Hoofdnavigatie", "Navigation principale"),
  "nav.more": s("More", "Mehr", "Meer", "Plus"),
  "nav.moreCurrent": s("{page} — more pages", "{page} — weitere Seiten", "{page} — meer pagina’s", "{page} — autres pages"),
  "nav.morePages": s("More pages", "Weitere Seiten", "Meer pagina’s", "Autres pages"),
  "nav.quickSettings": s("Quick settings", "Schnelleinstellungen", "Snelle instellingen", "Réglages rapides"),
  "nav.customize": s("Choose the pages in the bar", "Seiten der Leiste wählen", "Pagina’s in de balk kiezen", "Choisir les pages de la barre"),
  "nav.inBar": s("In the bar", "In der Leiste", "In de balk", "Dans la barre"),
  "nav.inMore": s("Under More", "Unter Mehr", "Onder Meer", "Sous Plus"),
  "nav.swapHint": s("Tap two pages to swap them.", "Tippe zwei Seiten an, um sie zu tauschen.", "Tik op twee pagina’s om ze te wisselen.", "Touchez deux pages pour les échanger."),
  "nav.swapWith": s("Now tap the page to swap with {page}.", "Tippe jetzt die Seite an, die mit {page} tauschen soll.", "Tik nu op de pagina die met {page} moet wisselen.", "Touchez maintenant la page à échanger avec {page}."),
  "nav.barNow": s("In the bar: {pages}", "In der Leiste: {pages}", "In de balk: {pages}", "Dans la barre : {pages}"),
  "nav.resetBar": s("Default pages", "Standardseiten", "Standaardpagina’s", "Pages par défaut"),
  "nav.signInRequired": s("Sign-in required", "Anmeldung erforderlich", "Aanmelding vereist", "Connexion requise"),
  "nav.needsAttention": s("needs attention", "braucht Aufmerksamkeit", "vraagt aandacht", "demande de l’attention"),
  "nav.showLabels": s("Show menu text", "Menütext einblenden", "Menutekst tonen", "Afficher le texte du menu"),
  "nav.hideLabels": s("Hide menu text", "Menütext ausblenden", "Menutekst verbergen", "Masquer le texte du menu"),
  "nav.classic": s("Classic view", "Klassische Ansicht", "Klassieke weergave", "Vue classique"),
  "nav.classicSwitch": s("Switch to classic view", "Zur klassischen Ansicht", "Naar klassieke weergave", "Passer à la vue classique"),
  "nav.about": s("About & licenses", "Über & Lizenzen", "Over & licenties", "À propos et licences"),
  "nav.about.short": s("About", "Über", "Over", "À propos"),
  "page.about.sub": s(
    "Version, source code and the licenses of what this page is built from",
    "Version, Quellcode und die Lizenzen dessen, woraus diese Seite besteht",
    "Versie, broncode en de licenties van waaruit deze pagina bestaat",
    "Version, code source et licences de ce qui compose cette page",
  ),

  "about.title": s("About", "Über", "Over", "À propos"),
  "about.version": s("Version", "Version", "Versie", "Version"),
  "about.revision": s("Commit", "Commit", "Commit", "Commit"),
  "about.development": s("development build", "Entwicklungsstand", "ontwikkelversie", "version de développement"),
  "about.source": s("Source code of this version", "Quellcode dieser Version", "Broncode van deze versie", "Code source de cette version"),
  "about.license.title": s("License", "Lizenz", "Licentie", "Licence"),
  "about.license.notice": s(
    "{product} is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.",
    "{product} ist freie Software: Du kannst sie unter den Bedingungen der GNU Affero General Public License, wie von der Free Software Foundation veröffentlicht, weitergeben und/oder verändern, entweder nach Version 3 der Lizenz oder (nach deiner Wahl) jeder späteren Version. Sie wird in der Hoffnung verbreitet, dass sie nützlich ist, aber OHNE JEDE GEWÄHRLEISTUNG, sogar ohne die implizite Gewährleistung der MARKTREIFE oder der EIGNUNG FÜR EINEN BESTIMMTEN ZWECK.",
    "{product} is vrije software: je mag het verspreiden en/of wijzigen onder de voorwaarden van de GNU Affero General Public License zoals gepubliceerd door de Free Software Foundation, versie 3 van de licentie of (naar jouw keuze) een latere versie. Het wordt verspreid in de hoop dat het nuttig is, maar ZONDER ENIGE GARANTIE, zelfs zonder de impliciete garantie van VERKOOPBAARHEID of GESCHIKTHEID VOOR EEN BEPAALD DOEL.",
    "{product} est un logiciel libre : vous pouvez le redistribuer et/ou le modifier selon les termes de la GNU Affero General Public License telle que publiée par la Free Software Foundation, soit la version 3 de la licence, soit (à votre choix) toute version ultérieure. Il est distribué dans l’espoir qu’il sera utile, mais SANS AUCUNE GARANTIE, sans même la garantie implicite de QUALITÉ MARCHANDE ou d’ADÉQUATION À UN USAGE PARTICULIER.",
  ),
  "about.license.text": s("GNU AGPL license text", "Lizenztext der GNU AGPL", "Licentietekst van de GNU AGPL", "Texte de la licence GNU AGPL"),
  "about.license.inventory": s(
    "Third-party components of the whole system",
    "Drittkomponenten des ganzen Systems",
    "Componenten van derden in het hele systeem",
    "Composants tiers de tout le système",
  ),
  "about.community.title": s("An independent community project", "Ein unabhängiges Community-Projekt", "Een onafhankelijk communityproject", "Un projet communautaire indépendant"),
  "about.community.text": s(
    "{product} is not developed, endorsed or supported by Zendure, and it is not a Zendure product. Zendure, SolarFlow and the other product names mentioned here are trademarks of their respective owners; this project uses them only to say which hardware it works with.",
    "{product} wird nicht von Zendure entwickelt, empfohlen oder unterstützt und ist kein Zendure-Produkt. Zendure, SolarFlow und die übrigen hier genannten Produktnamen sind Marken ihrer jeweiligen Inhaber; dieses Projekt nennt sie nur, um zu sagen, mit welcher Hardware es arbeitet.",
    "{product} wordt niet door Zendure ontwikkeld, aanbevolen of ondersteund en is geen product van Zendure. Zendure, SolarFlow en de andere hier genoemde productnamen zijn merken van hun respectieve eigenaren; dit project gebruikt ze alleen om te zeggen met welke hardware het werkt.",
    "{product} n’est ni développé, ni recommandé, ni pris en charge par Zendure, et ce n’est pas un produit Zendure. Zendure, SolarFlow et les autres noms de produits cités ici sont des marques de leurs propriétaires respectifs ; ce projet ne les emploie que pour indiquer le matériel avec lequel il fonctionne.",
  ),
  "about.community.help": s(
    "For help with this software, turn to GitHub, not to Zendure support: ask questions in the Discussions and report bugs as issues.",
    "Hilfe zu dieser Software bekommst du auf GitHub, nicht beim Zendure-Support: Fragen stellst du in den Discussions, Fehler meldest du als Issue.",
    "Hulp bij deze software vind je op GitHub, niet bij de support van Zendure: vragen stel je in de Discussions, fouten meld je als issue.",
    "Pour obtenir de l’aide sur ce logiciel, passez par GitHub plutôt que par le support Zendure : posez vos questions dans les Discussions et signalez un bug par une issue.",
  ),
  "about.discussions": s("Discussions on GitHub", "Discussions auf GitHub", "Discussions op GitHub", "Discussions sur GitHub"),
  "about.issues": s("Issues on GitHub", "Issues auf GitHub", "Issues op GitHub", "Issues sur GitHub"),
  "about.devices.title": s(
    "Does it work with your devices?",
    "Läuft es mit deinen Geräten?",
    "Werkt het met jouw apparaten?",
    "Cela fonctionne-t-il avec vos appareils ?",
  ),
  "about.devices.text": s(
    "{product} supports many Zendure devices and grid meters, but only a few of them have been tested on real hardware so far. If it works with yours, please say so in a short device report on GitHub: each report lets a device be marked as working in the list of supported setups, so the next person with the same device knows before buying it. A report about a device that does not work helps just as much.",
    "{product} unterstützt viele Zendure-Geräte und Netzzähler, aber nur wenige davon wurden bisher an echter Hardware getestet. Läuft es mit deinen, melde das bitte in einem kurzen Gerätebericht auf GitHub: Mit jedem Bericht kann ein Gerät in der Liste der unterstützten Setups als funktionierend markiert werden, damit die nächste Person mit demselben Gerät es schon vor dem Kauf weiß. Ein Bericht über ein Gerät, das nicht funktioniert, hilft genauso.",
    "{product} ondersteunt veel Zendure-apparaten en netmeters, maar slechts enkele daarvan zijn tot nu toe op echte hardware getest. Werkt het met de jouwe, meld dat dan in een kort apparaatrapport op GitHub: met elk rapport kan een apparaat in de lijst met ondersteunde opstellingen als werkend worden gemarkeerd, zodat de volgende met hetzelfde apparaat het al vóór de aankoop weet. Een rapport over een apparaat dat niet werkt, helpt net zo goed.",
    "{product} prend en charge de nombreux appareils Zendure et compteurs réseau, mais seuls quelques-uns ont été testés jusqu’ici sur du vrai matériel. Si cela fonctionne avec les vôtres, signalez-le dans un court rapport d’appareil sur GitHub : chaque rapport permet de marquer un appareil comme fonctionnel dans la liste des configurations prises en charge, pour que la prochaine personne équipée du même appareil le sache avant l’achat. Un rapport sur un appareil qui ne fonctionne pas aide tout autant.",
  ),
  "about.devices.report": s("Report a device on GitHub", "Gerät auf GitHub melden", "Apparaat melden op GitHub", "Signaler un appareil sur GitHub"),
  "about.devices.supported": s("Supported setups", "Unterstützte Setups", "Ondersteunde opstellingen", "Configurations prises en charge"),
  "about.components.title": s("Components in this page", "Bestandteile dieser Seite", "Onderdelen van deze pagina", "Composants de cette page"),
  "about.components.sub": s(
    "Third-party code, fonts and icons this page is built from, with their licenses",
    "Fremder Code, Schriften und Icons, aus denen diese Seite besteht, mit ihren Lizenzen",
    "Code, lettertypen en pictogrammen van derden waaruit deze pagina bestaat, met hun licenties",
    "Code, polices et icônes tiers qui composent cette page, avec leurs licences",
  ),
  "about.components.loading": s("Reading the notices …", "Die Hinweise werden gelesen …", "De vermeldingen worden gelezen …", "Lecture des mentions …"),
  "about.components.failed": s(
    "This notice could not be read: {file}",
    "Dieser Hinweis konnte nicht gelesen werden: {file}",
    "Deze vermelding kon niet worden gelezen: {file}",
    "Cette mention n’a pas pu être lue : {file}",
  ),

  "assistant.welcome": s("Welcome to EMS SolarFlow", "Willkommen bei EMS SolarFlow", "Welkom bij EMS SolarFlow", "Bienvenue dans EMS SolarFlow"),
  "assistant.next.title": s("Next steps", "Nächste Schritte", "Volgende stappen", "Prochaines étapes"),
  "assistant.next.progress": s("{done} of {total} done", "{done} von {total} erledigt", "{done} van {total} gedaan", "{done} sur {total} faites"),
  "assistant.next.allDone": s("Everything is set up.", "Alles ist eingerichtet.", "Alles is ingesteld.", "Tout est configuré."),
  "assistant.state.done": s("Done", "Erledigt", "Gedaan", "Fait"),
  "assistant.state.todo": s("To do", "Offen", "Te doen", "À faire"),
  "assistant.state.later": s("Later", "Später", "Later", "Plus tard"),
  "assistant.state.unknown": s("Unknown", "Unbekannt", "Onbekend", "Inconnu"),
  "assistant.state.off": s("Off", "Aus", "Uit", "Désactivé"),
  "assistant.state.ready": s("Ready", "Bereit", "Beschikbaar", "Disponible"),
  "assistant.step.later": s("Comes after an earlier step.", "Folgt nach einem früheren Schritt.", "Volgt na een eerdere stap.", "Vient après une étape précédente."),
  "assistant.step.unreadable": s("This could not be read just now.", "Das ließ sich gerade nicht lesen.", "Dit kon nu niet worden gelezen.", "Ceci n’a pas pu être lu pour l’instant."),
  "assistant.tasks.title": s("What would you like to do?", "Was möchtest du tun?", "Wat wil je doen?", "Que voulez-vous faire ?"),
  "assistant.keep": s("Show this page when the console opens", "Diese Seite beim Öffnen zeigen", "Deze pagina tonen bij het openen", "Afficher cette page à l’ouverture"),
  "assistant.toOverview": s("Go to the overview", "Zur Übersicht", "Naar het overzicht", "Aller à l’aperçu"),

  "auth.signIn": s("Sign in", "Anmelden", "Aanmelden", "Se connecter"),
  "auth.signOut": s("Sign out", "Abmelden", "Afmelden", "Se déconnecter"),
  "auth.signOutFailed": s("Sign-out failed", "Abmelden fehlgeschlagen", "Afmelden mislukt", "Échec de la déconnexion"),
  "auth.password": s("Password", "Passwort", "Wachtwoord", "Mot de passe"),
  "auth.cancel": s("Cancel", "Abbrechen", "Annuleren", "Annuler"),

  "appearance.title": s("Appearance", "Darstellung", "Weergave", "Apparence"),
  "appearance.theme": s("Colour scheme", "Farbschema", "Kleurenschema", "Thème"),
  "appearance.language": s("Language", "Sprache", "Taal", "Langue"),
  "appearance.density": s("Density", "Dichte", "Dichtheid", "Densité"),
  "appearance.density.normal": s("Normal", "Normal", "Normaal", "Normale"),
  "appearance.density.compact": s("Compact", "Kompakt", "Compact", "Compacte"),
  "appearance.nav": s("Side menu", "Seitenmenü", "Zijmenu", "Menu latéral"),
  "appearance.nav.auto": s("Auto", "Automatisch", "Automatisch", "Auto"),
  "appearance.nav.labels": s("With text", "Mit Text", "Met tekst", "Avec texte"),
  "appearance.nav.icons": s("Icons", "Symbole", "Pictogrammen", "Icônes"),
  "appearance.motion": s("Motion", "Animation", "Animatie", "Animations"),
  "appearance.motionServerOff": s("Animation is switched off for this installation.", "Animationen sind für diese Installation abgeschaltet.", "Animaties zijn voor deze installatie uitgeschakeld.", "Les animations sont désactivées pour cette installation."),

  "common.refresh": s("Refresh", "Aktualisieren", "Vernieuwen", "Actualiser"),
  "common.close": s("Close", "Schließen", "Sluiten", "Fermer"),
  "common.done": s("Done", "Fertig", "Klaar", "Terminé"),
  "common.retry": s("Try again", "Erneut versuchen", "Opnieuw proberen", "Réessayer"),
  "common.loading": s("Loading", "Lädt", "Laden", "Chargement"),
  "common.save": s("Save", "Speichern", "Opslaan", "Enregistrer"),
  "common.saved": s("Saved", "Gespeichert", "Opgeslagen", "Enregistré"),
  "common.saving": s("Saving", "Speichert", "Opslaan", "Enregistrement"),
  "common.on": s("On", "An", "Aan", "Activé"),
  "common.off": s("Off", "Aus", "Uit", "Désactivé"),
  "common.yes": s("Yes", "Ja", "Ja", "Oui"),
  "common.today": s("Today", "Heute", "Vandaag", "Aujourd’hui"),
  "common.details": s("Details", "Details", "Details", "Détails"),
  "common.all": s("All", "Alle", "Alle", "Tous"),

  "error.generic": s("Something went wrong ({code}).", "Etwas ist schiefgegangen ({code}).", "Er ging iets mis ({code}).", "Une erreur s’est produite ({code})."),
  "error.load": s("Could not load this data.", "Diese Daten konnten nicht geladen werden.", "Deze gegevens konden niet worden geladen.", "Impossible de charger ces données."),

  "diagnose.metric.ok": s("OK", "OK", "OK", "OK"),
  "diagnose.metric.warning": s("Warnings", "Warnungen", "Waarschuwingen", "Avertissements"),
  "diagnose.metric.error": s("Errors", "Fehler", "Fouten", "Erreurs"),
  "diagnose.metric.devices": s("Devices", "Geräte", "Apparaten", "Appareils"),
  "diagnose.status.ok": s("OK", "OK", "OK", "OK"),
  "diagnose.status.warning": s("Warning", "Warnung", "Waarschuwing", "Avertissement"),
  "diagnose.status.error": s("Error", "Fehler", "Fout", "Erreur"),
  "diagnose.status.unknown": s("Unknown", "Unbekannt", "Onbekend", "Inconnu"),
  "diagnose.status.info": s("Info", "Hinweis", "Info", "Info"),
  "diagnose.rootCauses": s("Root causes", "Ursachen", "Oorzaken", "Causes"),
  "diagnose.global.errors": s("Errors", "Fehler", "Fouten", "Erreurs"),
  "diagnose.global.warnings": s("Warnings", "Warnungen", "Waarschuwingen", "Avertissements"),
  "diagnose.sections": s("Sections", "Bereiche", "Onderdelen", "Sections"),
  "diagnose.sectionOk": s("No findings.", "Keine Befunde.", "Geen bevindingen.", "Aucune anomalie."),
  "diagnose.noSections": s("No sections reported.", "Keine Bereiche gemeldet.", "Geen onderdelen gemeld.", "Aucune section signalée."),
};
