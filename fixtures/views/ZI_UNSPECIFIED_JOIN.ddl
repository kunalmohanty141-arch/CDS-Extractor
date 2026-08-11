// R-10 inconclusive — a bare LEFT OUTER JOIN. The framework needs a to-one
// shape; this DDL neither declares nor denies one. ABAP does not validate
// cardinality at runtime, so the tool refuses to guess in either direction.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture: {
      mapping: [
        {table: 'SNWD_SO',   role: #MAIN,                   viewElement: ['SalesOrderGuid'], tableElement: ['NODE_KEY']},
        {table: 'SNWD_SO_I', role: #LEFT_OUTER_TO_ONE_JOIN, viewElement: ['ItemGuid'],       tableElement: ['NODE_KEY']}
      ] } } }
define view entity ZI_UNSPECIFIED_JOIN
  as select from snwd_so as SalesOrder
    left outer join snwd_so_i as Item on Item.parent_key = SalesOrder.node_key
{
  key SalesOrder.node_key as SalesOrderGuid,
      Item.node_key       as ItemGuid,
      Item.parent_key     as ItemParentGuid,
      SalesOrder.so_id    as SalesOrderId
}
