# Rizzo-IDA
Rizzo plugin ported to IDA 7.4+

## Usage

put `rizzo.py` into `${IDA-FOLDER}/plugins`.

## Description

An IDAPython plugin that generates "fuzzy" function signatures that can be shared and applied amongst different IDBs.

There are multiple sets of signatures that are generated:

* "Formal" signatures, where functions must match exactly
* "Fuzzy" signatures, where functions must only resemble each other in terms of data/call references.
* String-based signatures, where functions are identified based on unique string references.
* Immediate-based signatures, where functions are identified based on immediate value references.

These signatures are applied based on accuracy, that is, formal signatures are applied first, then string and immediate based signatures, and finally fuzzy signatures.

Further, functions are identified based on call references. Consider, for example, two functions, one named 'foo', the other named 'bar'.

The 'foo' function is fairly unique and a reliable signature is easily generated for it, but the 'bar' function is more difficult to reliably identify. However, 'foo' calls 'bar', and thus once 'foo' is identified, 'bar' can also be identified by association.

This version of Rizzo is ported to IDA 7.4+ by

    * Reverier-Xu from L-team, XDU, 2021-02-24

original authors:

    * Craig Heffner
    * devttys0
