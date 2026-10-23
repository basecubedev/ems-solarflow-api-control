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
  "nav.customizeDone": s("Done", "Fertig", "Klaar", "Terminé"),
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
