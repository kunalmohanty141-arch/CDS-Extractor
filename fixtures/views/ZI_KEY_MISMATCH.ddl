// R-17 — the #MAIN mapping names OrderId as the key, but the view marks
// Customer as its key instead. RSODP_ABAP_CDS 201 / KBA 3008492:
// "No representative key element found in CDS view".
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture: {
      mapping: [
        {table: 'ZCUSTORDER', role: #MAIN, viewElement: ['OrderId'], tableElement: ['ORDERID']}
      ] } } }
define view entity ZI_KEY_MISMATCH
  as select from zcustorder
{
  key customer  as Customer,
      orderid   as OrderId,
      orderdate as OrderDate
}
