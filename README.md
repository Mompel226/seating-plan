# Seating Plan

Draw your room once, then drag faces into chairs. It prints on one sheet.

> ## ⚠️ You need iSAMS
>
> This is built around exports from **iSAMS**, the school management system. **If your school does
> not use iSAMS, this will not work** — there is nowhere for it to get your class from.
>
> The photographs are the part that cannot be replaced: they come out of the iSAMS **Student ID
> Badge** report, and nothing else produces that file.
>
> If your school runs a different MIS it *may* still manage the names — the reader takes an
> Excel file and looks for `Surname` and `Forename` columns, accepting the usual variants
> (`Last Name`, `Given Name`, `Known As`, `Date of Birth`, `Form`, `Tutor Group`, `House`). That
> route is untested against anything but iSAMS, and it will not bring the photographs with it.
>
> Please check this before you spend time downloading it.

A seating plan is only useful if you can make one in the five minutes before a lesson and read it
from the front of the room. This is a small Mac app that does that and nothing else. It runs on your
own machine, keeps your classes in a folder you can see, and never sends a child's name or face
anywhere.

![The seating plan: six benches, twenty students, cards showing photograph, name and label](docs/seating-plan.png)

*Every name and face above is invented — the screenshot was taken with a made-up class.*

## What it does

- **Import a class from iSAMS.** Two exports is all it needs: the Export Wizard file for names,
  preferred names and dates of birth, and the Student ID Badge report for the photographs. Drop the
  files on the page; there is no wizard to sit through.
- **Draw the room.** Benches, tables, a whiteboard, a screen, the teacher's desk, the door. Put the
  chairs where the chairs actually are — six round a bench, two a side, one at the end.
- **Seat them.** Drag a face into a chair. Drag one onto another to swap the two. `Shuffle` for a
  fresh arrangement, `A–Z` for a register order, `Alternate` to break up whoever needs breaking up.
- **Keep several plans per class,** so the plan for a practical is not the plan for a test.
- **Label who needs what** — English confidence, a target, anything you want to see at a glance
  without writing it on the card in words a passing student can read.
- **Print it on one sheet,** or open *Student's view* and turn the plan round so the room sees it
  from where they are sitting rather than from the front.

## What you can choose to show

Each card can carry the photograph, the surname, the pupil's legal name in brackets when you call
them something else, and your own labels. A **New this year** badge is worked out from the iSAMS
export rather than set by hand, so it is right without you maintaining it.

## Privacy

This matters more than the features, so it is worth being plain about it.

- **Everything stays on your Mac.** The app is a small local web server that only ever listens on
  `127.0.0.1`, for your browser, on a random port, behind a token generated at launch. There is no
  account, no cloud, no telemetry, and no outbound request of any kind.
- **Your classes are a folder you can open.** `Classes/<class>/students.csv` and a `photos/` folder
  beside it. You can read them, back them up, or delete them in the Finder. Nothing is hidden in a
  database.
- **Photographs of children are the most sensitive thing here.** Treat the folder the way you would
  treat a printed class list with photographs on it: keep it on a machine only you use, and delete a
  class when you no longer teach it. The app will not stop you copying the folder somewhere
  careless; nothing can.
- **This repository contains no pupil data.** The screenshot above is an invented class, and the
  `Classes/` folder is ignored by git so a real one can never be committed by accident.

## Installing

Download the repository (**Code ▸ Download ZIP**), keep the folder together wherever you like —
Documents is fine — and double-click `Install.command`.

macOS will block it the first time, because the app is not signed by a paid Apple developer account.
That is expected. `READ ME FIRST.html` walks through the three clicks that get past it
( ▸ System Settings ▸ Privacy & Security ▸ **Open Anyway**). You do it once.

You get one Desktop icon, **Seating Plan**, which opens in its own window rather than a browser tab.

If you move the folder afterwards, run `Install.command` again — the icon points at wherever the
folder was when you installed.

`Uninstall.command` removes the Desktop icon and the background pieces. Your classes stay; delete
the folder yourself if you want them gone.

## Requirements

- **iSAMS** — see the notice at the top. Two exports: the **Export Wizard** file for names, preferred
  names and dates of birth, and the **Student ID Badge** report for the photographs. Both from
  Student Manager ▸ tick the class ▸ Search ▸ select all ▸ Selected Students ▸ Exporting and Reports.
  Choose **Excel** whenever iSAMS offers you a format.
- **macOS.** Built from Swift for the window and Python for the local server, both of which macOS
  already has — nothing to install first, and no dependencies to keep up to date.

## Made by

Dr Daniel Mompel Riera, Biology, NLCS Jeju — <dmompelriera@nlcsjeju.kr>

Free for any school to use, change, and pass on.
